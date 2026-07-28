-- 012: add_playlist_track takes the playlist row lock (deadlock fix, #55 review)
-- The bulk RPC (011) locks the playlists row before inserting; the single-add
-- RPC did not, and its AFTER trigger (bump_playlist_on_track_change) needs
-- that same row — overlapping writers could deadlock (single holds the unique
-- index entry and waits for the playlist lock; bulk holds the lock and waits
-- for the index entry). Rule: every playlist_tracks writer acquires the
-- playlist row lock first, so lock order is consistent. Also removes the
-- pre-existing race where two concurrent single adds read the same
-- MAX(position). Body otherwise identical to 001.

CREATE OR REPLACE FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid)
 RETURNS json
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_next_pos int;
  v_row playlist_tracks%ROWTYPE;
BEGIN
  -- serialize writers on this playlist (consistent lock order with 011)
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;

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