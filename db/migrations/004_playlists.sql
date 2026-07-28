-- 004: playlists domain (functions in use by the new backend)
-- Exported from Supabase on 2026-07-28 via pg_get_functiondef (verbatim).
-- The add_playlist_track RPC lives in 001 (not repeated here).
-- bump_playlist_updated_at: BEFORE UPDATE trigger on playlists, bumps only
--   when title/description/is_public change.
-- bump_playlist_on_track_change: trigger on playlist_tracks, bumps the
--   parent's updated_at on add/remove (move does it inside its own RPC).
-- cleanup_library_on_playlist_delete: trigger cleaning library_items
--   (kind=playlist, source=external) when a playlist is deleted.
-- move_playlist_track: atomic reorder RPC (used by
--   services/playlist_service.py move_track). Note: the final EXCEPTION
--   block returns SQLERRM in 'error'; the service surfaces it as
--   upstream_error and that text never reaches the client.
-- get_owned_playlists_with_track: owned-with-track RPC, takes the provider
--   id (p_track_id text) and resolves the internal uuid inside.
-- get_user_playlist_thumbnails: up to 4 thumbnails per user playlist
--   (reads playlist_tracks).

CREATE OR REPLACE FUNCTION public.bump_playlist_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
  if (new.title, new.description, new.is_public)
     is distinct from (old.title, old.description, old.is_public) then
    new.updated_at = now();
  end if;
  return new;
end;
$function$;

CREATE OR REPLACE FUNCTION public.bump_playlist_on_track_change()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
  if pg_trigger_depth() > 1 then
    return null;
  end if;

  update playlists
     set updated_at = now()
   where id = coalesce(new.playlist_id, old.playlist_id);
  return null;
end;
$function$;

CREATE OR REPLACE FUNCTION public.cleanup_library_on_playlist_delete()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
  delete from public.library_items
   where kind = 'playlist'
     and source = 'external'
     and external_id = old.id::text;
  return old;
end;
$function$;

CREATE OR REPLACE FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_total INT;
  v_rows JSON;
  v_row RECORD;
  v_idx INT;
BEGIN
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

CREATE OR REPLACE FUNCTION public.get_owned_playlists_with_track(p_user_id uuid, p_track_id text)
 RETURNS TABLE(playlist_id uuid)
 LANGUAGE sql
 STABLE
AS $function$
    SELECT pl.id
    FROM playlists pl
    JOIN playlist_tracks pt ON pt.playlist_id = pl.id
    JOIN tracks t ON t.id = pt.track_id
    WHERE pl.owner_id = p_user_id
      AND t.track_id = p_track_id;
$function$;

CREATE OR REPLACE FUNCTION public.get_user_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer DEFAULT 4)
 RETURNS TABLE(playlist_id uuid, thumbnail_url text)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT
    upt.playlist_id,
    t.thumbnail_url
  FROM (
    SELECT
      playlist_id,
      track_id,
      ROW_NUMBER() OVER (PARTITION BY playlist_id ORDER BY position) AS rn
    FROM playlist_tracks
    WHERE playlist_id = ANY(playlist_ids)
  ) upt
  JOIN tracks t ON t.id = upt.track_id
  WHERE upt.rn <= limit_per_playlist
    AND t.thumbnail_url IS NOT NULL
    AND t.thumbnail_url <> ''
  ORDER BY upt.playlist_id, upt.rn;
$function$;