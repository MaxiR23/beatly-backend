-- 018: move_playlist_track checks caller ownership before reordering (#90)
-- move_playlist_track is SECURITY DEFINER, so it runs as the function
-- owner (postgres) and bypasses RLS. 017 already revoked EXECUTE from
-- PUBLIC and anon on it (2026-09-04, #88), but the GRANT to authenticated
-- is still in place (017 lines 2838-2840) and the function itself never
-- checked who was calling it: an authenticated caller hitting the RPC
-- directly through PostgREST, without going through this backend, could
-- reorder a playlist that is not theirs just by knowing its id. README
-- finding 7(b) already flagged this hole.
--
-- This backend calls the RPC exclusively with the service-role client
-- (core/database.py has no user-scoped / RLS-aware client), so auth.uid()
-- is NULL on every legitimate call this backend makes. A plain
-- auth.uid() = owner_id check would break all of them. The same dilemma
-- is already solved in this schema by prevent_role_self_update() (003,
-- reflected in 017 lines 889-912): it treats a NULL auth.uid() as a
-- trusted caller (service role / internal trigger) and only enforces the
-- check when there is a real user JWT behind the call. This migration
-- copies that passthrough.
--
-- Also adds the IF NOT FOUND branch that add_playlist_track (014) and
-- remove_playlist_track (013) already have, so a playlist that no longer
-- exists returns playlist_not_found instead of falling through to
-- empty_playlist. The rest of the body -- index clamping, the temp-table
-- reorder, the updated_at bump on the parent, the final json_agg and the
-- EXCEPTION WHEN OTHERS handler (SQLERRM exposure is README finding 4,
-- known debt, untouched here) -- is verbatim from 013, comments in
-- Spanish included. The signature is unchanged so the GRANT/REVOKE
-- already applied in 017 keep covering this function without needing to
-- be reapplied.
--
-- The search_path is set to 'public', 'pg_temp' rather than the bare
-- 'public' the other five SECURITY DEFINER functions in 017 use
-- (is_admin, handle_new_user, prevent_role_self_update,
-- is_developer_or_higher, is_tester_or_higher). None of those five
-- touches a temporary table; this one does. When pg_temp is not listed
-- explicitly, PostgreSQL searches the temporary schema FIRST for
-- relation names, so a caller with direct SQL access could create a temp
-- table named playlists and shadow the very row this function reads to
-- decide ownership. Listing pg_temp last pins it behind public and
-- closes that, at no cost: CREATE TEMP TABLE tmp_reorder targets pg_temp
-- through the TEMP keyword regardless of the path, and the later
-- references to tmp_reorder still resolve there because no
-- public.tmp_reorder exists. The body below is unchanged by this.
--
-- Note for whoever changes the permission model later: until now
-- can_edit() in services/playlist_service.py was the single place that
-- decided who may edit a playlist. This function is a second one, in
-- SQL. Collaborative playlists, or any other loosening of that rule,
-- have to be applied in both.

CREATE OR REPLACE FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
  v_total INT;
  v_rows JSON;
  v_row RECORD;
  v_idx INT;
  v_owner_id uuid;
BEGIN
  -- serialize writers on this playlist (write protocol, see 013)
  SELECT owner_id INTO v_owner_id
  FROM playlists WHERE id = p_playlist_id FOR UPDATE;

  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  IF auth.uid() IS NOT NULL AND auth.uid() <> v_owner_id THEN
    RETURN json_build_object('ok', false, 'error', 'forbidden');
  END IF;

  -- Defer constraint para permitir posiciones temporales duplicadas
  SET CONSTRAINTS ux_playlist_pos DEFERRED;

  -- Contar tracks
  SELECT COUNT(*) INTO v_total
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  IF v_total = 0 THEN
    RETURN json_build_object('ok', false, 'error', 'empty_playlist');
  END IF;

  -- Clamp indices
  IF p_old_index < 1 THEN p_old_index := 1; END IF;
  IF p_old_index > v_total THEN p_old_index := v_total; END IF;
  IF p_new_index < 1 THEN p_new_index := 1; END IF;
  IF p_new_index > v_total THEN p_new_index := v_total; END IF;

  -- Noop
  IF p_old_index = p_new_index THEN
    SELECT json_agg(row_to_json(t) ORDER BY t.position)
    INTO v_rows
    FROM (
      SELECT id AS "internalId", track_id AS "trackId", position
      FROM playlist_tracks
      WHERE playlist_id = p_playlist_id
      ORDER BY position
    ) t;

    RETURN json_build_object('ok', true, 'noop', true, 'order', v_rows);
  END IF;

  -- Crear tabla temporal con el orden actual
  CREATE TEMP TABLE tmp_reorder AS
  SELECT id, track_id, ROW_NUMBER() OVER (ORDER BY position) AS rn
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  -- Mover a posiciones negativas temporales (basadas en el nuevo orden)
  IF p_new_index < p_old_index THEN
    -- Mover hacia arriba: el movido va a new_index, los demas bajan
    UPDATE playlist_tracks pt
    SET position = CASE
      WHEN t.rn = p_old_index THEN -p_new_index
      WHEN t.rn >= p_new_index AND t.rn < p_old_index THEN -(t.rn + 1)
      ELSE -t.rn
    END
    FROM tmp_reorder t
    WHERE pt.id = t.id;
  ELSE
    -- Mover hacia abajo: el movido va a new_index, los demas suben
    UPDATE playlist_tracks pt
    SET position = CASE
      WHEN t.rn = p_old_index THEN -p_new_index
      WHEN t.rn > p_old_index AND t.rn <= p_new_index THEN -(t.rn - 1)
      ELSE -t.rn
    END
    FROM tmp_reorder t
    WHERE pt.id = t.id;
  END IF;

  -- Convertir posiciones negativas a positivas
  UPDATE playlist_tracks
  SET position = -position
  WHERE playlist_id = p_playlist_id AND position < 0;

  -- Limpiar
  DROP TABLE tmp_reorder;

  -- Bump updated_at del padre: paso dentro del MISMO RPC, asi es atomico
  -- con el reorder. Si el reorder commitea, esto commitea; si falla, ambos.
  UPDATE playlists SET updated_at = now() WHERE id = p_playlist_id;

  -- Devolver orden final
  SELECT json_agg(row_to_json(t) ORDER BY t.position)
  INTO v_rows
  FROM (
    SELECT id AS "internalId", track_id AS "trackId", position
    FROM playlist_tracks
    WHERE playlist_id = p_playlist_id
    ORDER BY position
  ) t;

  RETURN json_build_object('ok', true, 'order', v_rows);

EXCEPTION WHEN OTHERS THEN
  RETURN json_build_object('ok', false, 'error', SQLERRM);
END;
$function$;
