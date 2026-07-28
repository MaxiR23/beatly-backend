-- 005: playlists — old position mechanism (status verified 2026-07-28)
-- Exported from Supabase on 2026-07-28 via pg_get_functiondef (verbatim).
-- Status after checking pg_trigger and the trigger export (see 009):
--   playlist_tracks_reorder: ACTIVE. Bound to playlist_tracks as
--     trg_playlist_tracks_reorder (BEFORE INSERT OR DELETE OR UPDATE,
--     FOR EACH ROW). It is live position maintenance, not dead legacy:
--     its DELETE branch closes the position gap when the service removes
--     a track, so the new backend depends on it without calling it. Its
--     INSERT branch is a no-op for the add_playlist_track RPC (which
--     appends at MAX+1). Do NOT drop.
--   move_track_position: no trigger binding, replaced by
--     move_playlist_track. Still unconfirmed whether anything calls it
--     (grep the legacy repo). Suspected dead — do not drop until checked.
--   update_positions: same as above, no known callers, suspected dead —
--     do not drop until checked.

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

CREATE OR REPLACE FUNCTION public.move_track_position(p_playlist_id uuid, p_old integer, p_new integer)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_row_id  uuid;
  v_total   integer;
  v_new     integer := p_new;
  v_tmppos  bigint := - (EXTRACT(EPOCH FROM clock_timestamp())::bigint); -- temp negativo único
BEGIN
  IF p_old < 1 OR p_new < 1 THEN
    RAISE EXCEPTION 'invalid positions (must be >=1)';
  END IF;

  -- total para clamp
  SELECT COUNT(*) INTO v_total
  FROM public.playlist_tracks
  WHERE playlist_id = p_playlist_id;

  IF v_total = 0 THEN
    RAISE EXCEPTION 'playlist empty';
  END IF;

  IF v_new > v_total THEN
    v_new := v_total;
  END IF;

  IF v_new = p_old THEN
    RETURN; -- nada que hacer
  END IF;

  -- fila a mover (lock fila)
  SELECT id INTO v_row_id
  FROM public.playlist_tracks
  WHERE playlist_id = p_playlist_id AND position = p_old
  FOR UPDATE;

  IF v_row_id IS NULL THEN
    RAISE EXCEPTION 'track_not_found at position %', p_old;
  END IF;

  -- A) estacionar la fila en un valor temporal negativo (evita UNIQUE)
  UPDATE public.playlist_tracks
  SET position = v_tmppos
  WHERE id = v_row_id;

  -- B) shift del rango afectado
  IF v_new < p_old THEN
    -- subir: [v_new, p_old-1] => +1
    UPDATE public.playlist_tracks
    SET position = position + 1
    WHERE playlist_id = p_playlist_id
      AND position BETWEEN v_new AND p_old - 1;
  ELSE
    -- bajar: [p_old+1, v_new] => -1
    UPDATE public.playlist_tracks
    SET position = position - 1
    WHERE playlist_id = p_playlist_id
      AND position BETWEEN p_old + 1 AND v_new;
  END IF;

  -- C) dejar la fila en su destino
  UPDATE public.playlist_tracks
  SET position = v_new
  WHERE id = v_row_id;

  -- D) saneo final por las dudas: reenumerar 1..N sin huecos
  WITH ord AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY position) AS rn
    FROM public.playlist_tracks
    WHERE playlist_id = p_playlist_id
  )
  UPDATE public.playlist_tracks t
  SET position = o.rn
  FROM ord o
  WHERE t.id = o.id;

END;
$function$;

CREATE OR REPLACE FUNCTION public.update_positions(p_playlist_id uuid, p_start integer, p_end integer, p_delta integer)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
BEGIN
  UPDATE playlist_tracks
  SET position = position + p_delta
  WHERE playlist_id = p_playlist_id
    AND position BETWEEN p_start AND p_end;
END;
$function$;