-- 001: unique constraint + atomic add RPC for playlist_tracks
-- Applied by hand in Supabase on 2026-07-27 (issue #52).
-- Fixes: duplicate concurrent adds (unique constraint) and the position
-- race on concurrent adds (position calc + insert in one atomic op).
-- Used by: services/playlist_service.py (add_track, add_tracks_bulk).

ALTER TABLE public.playlist_tracks
ADD CONSTRAINT ux_playlist_track UNIQUE (playlist_id, track_id);

CREATE OR REPLACE FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid)
 RETURNS json
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_next_pos int;
  v_row playlist_tracks%ROWTYPE;
BEGIN
  -- posicion siguiente e insert en una sola operacion atomica
  SELECT COALESCE(MAX(position), 0) + 1
  INTO v_next_pos
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  INSERT INTO playlist_tracks (playlist_id, track_id, position, added_by)
  VALUES (p_playlist_id, p_track_id, v_next_pos, p_added_by)
  RETURNING * INTO v_row;

  RETURN json_build_object('ok', true, 'id', v_row.id, 'position', v_row.position);

EXCEPTION
  WHEN unique_violation THEN
    -- el track ya esta en la playlist (constraint ux_playlist_track)
    RETURN json_build_object('ok', false, 'error', 'track_already_in_playlist');
END;
$function$;