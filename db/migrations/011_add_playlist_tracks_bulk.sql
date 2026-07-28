-- 011: set-based bulk add RPC for playlist_tracks (issue #55)
-- Replaces the per-track loop (up to 200 round trips, non-atomic) with a
-- single atomic RPC: one round trip, all-or-nothing.
-- Design notes:
--   * FOR UPDATE on the playlists row serializes concurrent writers on the
--     same playlist (two bulks cannot read the same MAX(position)) and
--     doubles as an existence check.
--   * Input array is deduped keeping the first occurrence, then tracks
--     already in the playlist are filtered out, then the remainder is
--     numbered 1..N in input order so positions come out contiguous with
--     no gaps even when some tracks are skipped.
--   * ON CONFLICT targets ux_playlist_track explicitly, as a safety net
--     for the residual race with writers that do not take the playlist
--     lock (e.g. the single-add RPC). A conflict there can leave a
--     position gap; acceptable, nothing breaks (move orders by position,
--     not by contiguity).
--   * Interaction with trg_playlist_tracks_reorder (BEFORE INSERT, per
--     row): rows are inserted in ascending position order and every new
--     position is greater than all existing ones at that moment, so the
--     trigger's shift UPDATE matches nothing — no-op by construction.
--   * No EXCEPTION block: unexpected errors must escape to the client
--     library and surface as upstream_error (no SQLERRM in the payload).

CREATE OR REPLACE FUNCTION public.add_playlist_tracks_bulk(
  p_playlist_id uuid,
  p_track_ids uuid[],
  p_added_by uuid
)
RETURNS json
LANGUAGE plpgsql
AS $function$
DECLARE
  v_base_pos int;
  v_added int;
  v_unique_requested int;
BEGIN
  -- serialize writers on this playlist + existence check
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  SELECT COALESCE(MAX(position), 0)
  INTO v_base_pos
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  -- input: dedupe the array keeping the first occurrence
  -- to_insert: drop tracks already in the playlist, number the rest 1..N
  --            in input order
  WITH input AS (
    SELECT DISTINCT ON (tid) tid, ord
    FROM unnest(p_track_ids) WITH ORDINALITY AS u(tid, ord)
    ORDER BY tid, ord
  ),
  to_insert AS (
    SELECT i.tid, ROW_NUMBER() OVER (ORDER BY i.ord) AS rn
    FROM input i
    WHERE NOT EXISTS (
      SELECT 1 FROM playlist_tracks pt
      WHERE pt.playlist_id = p_playlist_id
        AND pt.track_id = i.tid
    )
  )
  INSERT INTO playlist_tracks (playlist_id, track_id, position, added_by)
  SELECT p_playlist_id, tid, v_base_pos + rn, p_added_by
  FROM to_insert
  ORDER BY rn
  ON CONFLICT ON CONSTRAINT ux_playlist_track DO NOTHING;

  GET DIAGNOSTICS v_added = ROW_COUNT;

  SELECT COUNT(DISTINCT tid)
  INTO v_unique_requested
  FROM unnest(p_track_ids) AS u(tid);

  RETURN json_build_object(
    'ok', true,
    'added', v_added,
    'skipped', v_unique_requested - v_added
  );
END;
$function$;