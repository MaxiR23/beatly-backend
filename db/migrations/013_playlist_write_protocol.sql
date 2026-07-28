-- 013: playlist_tracks write protocol (#55 review, P1/P2)
-- THE RULE: every writer of playlist_tracks is an RPC, and every such RPC
-- takes the parent playlists row lock (SELECT ... FOR UPDATE) as its first
-- statement. Consistent parent-then-child lock order across all writers is
-- the standard way to make deadlocks impossible by construction.
--
-- Why not in the trigger: PostgreSQL locks the target row BEFORE running
-- BEFORE ROW UPDATE/DELETE triggers (GetTupleForTrigger, see pgsql-hackers
-- discussion by Alvaro Herrera, 2019-09-05), so a per-row trigger acquires
-- the parent lock too late and cannot enforce the order. The earlier
-- attempt to do so is reverted here: playlist_tracks_reorder goes back to
-- its 005 body, verbatim.
--
-- Writer status after this migration:
--   add_playlist_track        locks parent (012)
--   add_playlist_tracks_bulk  locks parent (011)
--   move_playlist_track       locks parent (this file)
--   remove_playlist_track     NEW RPC, locks parent (this file) — replaces
--                             the service's direct DELETE, the last write
--                             path outside the protocol
-- No write path to playlist_tracks remains outside the protocol.

-- 1) Revert: playlist_tracks_reorder back to 005 verbatim (no parent lock)

CREATE OR REPLACE FUNCTION public.playlist_tracks_reorder()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  IF pg_trigger_depth() > 1 THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
  END IF;

  IF TG_OP = 'INSERT' THEN
    -- posición nula -> al final (seteo directo en NEW, sin UPDATE por id)
    IF NEW.position IS NULL THEN
      SELECT COALESCE(MAX(position),0) + 1
        INTO NEW.position
        FROM public.playlist_tracks
       WHERE playlist_id = NEW.playlist_id;
      RETURN NEW;
    END IF;

    -- insertar en P -> correr a derecha todos los >= P (la fila aún no existe en tabla)
    UPDATE public.playlist_tracks
       SET position = position + 1
     WHERE playlist_id = NEW.playlist_id
       AND position >= NEW.position;

    RETURN NEW;

  ELSIF TG_OP = 'UPDATE' THEN
    IF NEW.playlist_id = OLD.playlist_id AND NEW.position = OLD.position THEN
      RETURN NEW;
    END IF;

    IF NEW.playlist_id = OLD.playlist_id THEN
      -- mover dentro de la misma playlist
      IF NEW.position IS NULL THEN
        SELECT COALESCE(MAX(position),0) + 1
          INTO NEW.position
          FROM public.playlist_tracks
         WHERE playlist_id = NEW.playlist_id;
        RETURN NEW;
      END IF;

      IF NEW.position < OLD.position THEN
        UPDATE public.playlist_tracks
           SET position = position + 1
         WHERE playlist_id = NEW.playlist_id
           AND ctid <> NEW.ctid
           AND position >= NEW.position
           AND position <  OLD.position;
      ELSE
        UPDATE public.playlist_tracks
           SET position = position - 1
         WHERE playlist_id = NEW.playlist_id
           AND ctid <> NEW.ctid
           AND position <= NEW.position
           AND position >  OLD.position;
      END IF;

      RETURN NEW;

    ELSE
      -- cambio de playlist: cerrar hueco en la vieja
      UPDATE public.playlist_tracks
         SET position = position - 1
       WHERE playlist_id = OLD.playlist_id
         AND position > OLD.position;

      -- decidir posición en la nueva
      IF NEW.position IS NULL THEN
        SELECT COALESCE(MAX(position),0) + 1
          INTO NEW.position
          FROM public.playlist_tracks
         WHERE playlist_id = NEW.playlist_id;
      ELSE
        UPDATE public.playlist_tracks
           SET position = position + 1
         WHERE playlist_id = NEW.playlist_id
           AND position >= NEW.position;
      END IF;

      RETURN NEW;
    END IF;

  ELSIF TG_OP = 'DELETE' THEN
    UPDATE public.playlist_tracks
       SET position = position - 1
     WHERE playlist_id = OLD.playlist_id
       AND position > OLD.position;
    RETURN OLD;
  END IF;

  RETURN NEW;
END
$function$;

-- 2) move_playlist_track: parent lock as first statement.
--    Body otherwise verbatim from 004 (including the SQLERRM return —
--    known debt, untouched here).

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
  -- serialize writers on this playlist (write protocol, see header)
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;

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

-- 3) remove_playlist_track: NEW. Replaces the service's direct DELETE —
--    the last playlist_tracks write path outside the protocol. Takes the
--    provider id (text) and resolves the internal uuid inside, per the
--    track identity rule. The DELETE fires the reorder trigger (closes
--    the position gap) and the bump trigger (parent row already locked by
--    this same transaction, so no wait). Idempotent: deleting a track not
--    in the playlist returns ok with deleted=0.

CREATE OR REPLACE FUNCTION public.remove_playlist_track(
  p_playlist_id uuid,
  p_track_id text
)
RETURNS json
LANGUAGE plpgsql
AS $function$
DECLARE
  v_deleted int;
BEGIN
  -- serialize writers on this playlist (write protocol, see header)
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  DELETE FROM playlist_tracks pt
  USING tracks t
  WHERE pt.playlist_id = p_playlist_id
    AND t.id = pt.track_id
    AND t.track_id = p_track_id;

  GET DIAGNOSTICS v_deleted = ROW_COUNT;

  RETURN json_build_object('ok', true, 'deleted', v_deleted);
END;
$function$;