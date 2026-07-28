-- 014: add_playlist_track returns playlist_not_found when the playlist
-- is gone (#63)
-- 012 added the parent lock but no IF NOT FOUND branch: if the playlist
-- vanishes between the service's permission check and this RPC, the
-- insert hits a foreign-key violation and escapes as a 502. Bulk (011)
-- and remove (013) already answer playlist_not_found -> 404 in the same
-- window; this aligns the single add. Body otherwise identical to 012.

CREATE OR REPLACE FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid)
 RETURNS json
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_next_pos int;
  v_row playlist_tracks%ROWTYPE;
BEGIN
  -- serialize writers on this playlist (write protocol, see 013)
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

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