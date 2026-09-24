-- 029: drop playlist_tracks.position, its trigger and its index/constraint,
-- and switch add_playlist_track, add_playlist_tracks_bulk and
-- move_playlist_track to compute the position they return instead of
-- reading or writing it (#137, stage 3 of 3)
--
-- Order inside this transaction, and why:
-- 1. A DO block, copied verbatim from 028, asserts, for every playlist,
--    that the order by order_key matches the order by position -- the
--    same gate 028 already runs before it makes order_key NOT NULL. This
--    runs immediately before DROP COLUMN position below, the only place
--    the old order is ever destroyed, inside the same transaction: if the
--    two orders ever disagree, this file must not proceed. Unlike 028,
--    the remedy on failure is not to re-run a backfill (027's backfill
--    stopped being safe to run the moment 028 started writing order_key
--    itself) -- it is to investigate which write to playlist_tracks left
--    order_key out of sync with position since 028 was applied, and not
--    apply this migration until that is resolved.
-- 2. DROP TRIGGER trg_playlist_tracks_reorder and DROP FUNCTION
--    playlist_tracks_reorder() -- the trigger is disabled live (ADR 007)
--    and its only job was keeping position dense; with position gone
--    neither has a reader left.
-- 3. ALTER TABLE ... DROP CONSTRAINT ux_playlist_pos, then DROP INDEX
--    idx_playlisttracks_playlist_pos, then ALTER TABLE ... DROP COLUMN
--    position, in that order and no other: DROP COLUMN removes any index
--    or constraint that still refers to the column automatically, so the
--    explicit drops have to run first -- if position went first, the two
--    DROP CONSTRAINT/DROP INDEX statements after it would fail with "does
--    not exist" instead of confirming what was actually removed. No
--    CASCADE anywhere: if anything outside this table still depended on
--    position, this must fail and roll back the whole transaction, not
--    silently take something else down with it.
-- 4. add_playlist_track, add_playlist_tracks_bulk and move_playlist_track
--    are redefined with CREATE OR REPLACE FUNCTION, not DROP + CREATE:
--    unlike 028, none of the three signatures changes here (still
--    add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by
--    uuid, p_order_key text), add_playlist_tracks_bulk(p_playlist_id
--    uuid, p_track_ids uuid[], p_added_by uuid, p_order_keys text[]),
--    move_playlist_track(p_playlist_id uuid, p_old_index integer,
--    p_new_index integer, p_order_key text), all RETURNS json), so
--    CREATE OR REPLACE keeps every grant and revoke 028 already applied
--    without needing to repeat any GRANT or REVOKE. It does not keep
--    every attribute, though: CREATE OR REPLACE FUNCTION resets anything
--    not restated, so move_playlist_track's SECURITY DEFINER and SET
--    search_path TO 'public', 'pg_temp' are copied verbatim below --
--    omitting either would silently drop the function back to SECURITY
--    INVOKER or an unpinned search_path with no error at apply time.
--
-- Why the JSON key 'position' survives in add_playlist_track's and
-- move_playlist_track's return value: the repo owner decided the API
-- keeps returning position, computed as the 1-based index of the track
-- in the order the corresponding GET returns, not a stored value (see
-- docs/api/playlists.md). add_playlist_track computes it with a
-- SELECT COUNT(*) taken under the same playlist lock, right after the
-- insert; move_playlist_track's two 'order' subqueries compute it with
-- ROW_NUMBER() OVER (ORDER BY order_key) instead of selecting the
-- dropped column. Neither RPC reads, writes or orders by position
-- anywhere in this file.
--
-- Order of application: the PR for #137 has to be merged, so the backend
-- runs the code from services/playlist_service.py this change ships,
-- before this file is applied -- that code no longer reads "position"
-- from playlist_tracks, only "track_id" and "order_key", and the
-- add_playlist_track it calls already returns the 'position' key against
-- the still-028 database. Code from before that merge still reads
-- "track_id, position" and gets a 502 the moment this file drops the
-- column, so it must not still be running when this file is applied.
-- This file itself can be applied at any point after the merge; nothing
-- here depends on when.
--
-- Locks: DROP TRIGGER takes SHARE ROW EXCLUSIVE on playlist_tracks;
-- ALTER TABLE ... DROP CONSTRAINT, DROP INDEX and ALTER TABLE ... DROP
-- COLUMN each take ACCESS EXCLUSIVE on playlist_tracks until COMMIT. This
-- file never touches playlists, so there is no reverse lock order
-- against the write protocol of 013.
--
-- Untouched: remove_playlist_track (current body in 017), get_
-- user_playlist_thumbnails (current body in 028, already orders by
-- order_key), get_playlist_thumbnails, recommend_playlists_by_history
-- (both current body in 023, both read genre_playlist_tracks, a
-- different table with its own position column that this file does not
-- touch) and genre_playlist_tracks itself.
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 028.

BEGIN;

DO $$
DECLARE
  v_mismatches bigint;
BEGIN
  SELECT count(*) INTO v_mismatches
  FROM (
    SELECT
      row_number() OVER (PARTITION BY playlist_id ORDER BY position) AS by_position,
      row_number() OVER (PARTITION BY playlist_id ORDER BY order_key COLLATE "C") AS by_key
    FROM public.playlist_tracks
  ) t
  WHERE by_position <> by_key;

  IF v_mismatches > 0 THEN
    RAISE EXCEPTION 'playlist_tracks: % row(s) where the order_key order does not match the position order -- investigate which write to playlist_tracks left order_key out of sync since 028 was applied, and do not apply this migration until it is resolved', v_mismatches;
  END IF;
END;
$$;

DROP TRIGGER trg_playlist_tracks_reorder ON public.playlist_tracks;

DROP FUNCTION public.playlist_tracks_reorder();

ALTER TABLE public.playlist_tracks
  DROP CONSTRAINT ux_playlist_pos;

DROP INDEX public.idx_playlisttracks_playlist_pos;

ALTER TABLE public.playlist_tracks
  DROP COLUMN position;

CREATE OR REPLACE FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid, p_order_key text)
 RETURNS json
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_position int;
  v_row playlist_tracks%ROWTYPE;
  v_constraint text;
BEGIN
  -- serialize writers on this playlist (write protocol, see 013)
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  INSERT INTO playlist_tracks (playlist_id, track_id, added_by, order_key)
  VALUES (p_playlist_id, p_track_id, p_added_by, p_order_key)
  RETURNING * INTO v_row;

  -- the caller read its neighbour outside this lock: confirm the key it
  -- computed still lands strictly after every other row's key
  IF EXISTS (
    SELECT 1 FROM playlist_tracks
    WHERE playlist_id = p_playlist_id AND id <> v_row.id AND order_key >= p_order_key
  ) THEN
    RAISE EXCEPTION 'order_key % is not strictly after every other row', p_order_key
      USING ERRCODE = 'unique_violation', CONSTRAINT = 'ux_playlist_order_key';
  END IF;

  -- the guard above leaves the new row strictly last by order_key, so its
  -- 1-based index is the row count, taken under the lock after the insert:
  -- the same index GET /playlists/{id} computes; nothing stores it
  SELECT COUNT(*)
  INTO v_position
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  RETURN json_build_object('ok', true, 'id', v_row.id, 'position', v_position);

EXCEPTION
  WHEN unique_violation THEN
    GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
    IF v_constraint = 'ux_playlist_order_key' THEN
      RETURN json_build_object('ok', false, 'error', 'order_key_conflict');
    END IF;
    -- the track is already in the playlist (constraint ux_playlist_track)
    RETURN json_build_object('ok', false, 'error', 'track_already_in_playlist');
END;
$function$;

CREATE OR REPLACE FUNCTION public.add_playlist_tracks_bulk(p_playlist_id uuid, p_track_ids uuid[], p_added_by uuid, p_order_keys text[])
 RETURNS json
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_ids uuid[];
  v_added int;
  v_unique_requested int;
  v_constraint text;
BEGIN
  -- serialize writers on this playlist + existence check
  PERFORM 1 FROM playlists WHERE id = p_playlist_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN json_build_object('ok', false, 'error', 'playlist_not_found');
  END IF;

  -- input: dedupe the array keeping the first occurrence, carrying its
  -- order_key along by ordinal position (unnest of two arrays is valid
  -- in the FROM clause)
  -- to_insert: drop tracks already in the playlist, number the rest 1..N
  --            in input order
  -- ins: the actual write, returning the id of every row this call wrote
  --      (skipped rows, via ON CONFLICT DO NOTHING, return no id)
  WITH input AS (
    SELECT DISTINCT ON (tid) tid, okey, ord
    FROM unnest(p_track_ids, p_order_keys) WITH ORDINALITY AS u(tid, okey, ord)
    ORDER BY tid, ord
  ),
  to_insert AS (
    SELECT i.tid, i.okey, ROW_NUMBER() OVER (ORDER BY i.ord) AS rn
    FROM input i
    WHERE NOT EXISTS (
      SELECT 1 FROM playlist_tracks pt
      WHERE pt.playlist_id = p_playlist_id
        AND pt.track_id = i.tid
    )
  ),
  ins AS (
    INSERT INTO playlist_tracks (playlist_id, track_id, added_by, order_key)
    SELECT p_playlist_id, tid, p_added_by, okey
    FROM to_insert
    ORDER BY rn
    ON CONFLICT ON CONSTRAINT ux_playlist_track DO NOTHING
    RETURNING id
  )
  SELECT COALESCE(array_agg(id), '{}'), count(*) INTO v_ids, v_added FROM ins;

  SELECT COUNT(DISTINCT tid)
  INTO v_unique_requested
  FROM unnest(p_track_ids) AS u(tid);

  -- the caller read its neighbour outside this lock: confirm every key it
  -- computed still lands strictly after every row this call did not just
  -- write. The rows this call wrote are identified by id, from v_ids
  -- (the INSERT's own RETURNING above) -- the same way add and move
  -- identify their own row, and not by matching order_key values: two
  -- concurrent writers reading the same last-seen key compute the same
  -- keys, so an existing row's order_key can equal one of this batch's
  -- keys without this batch having written it. NULL v_ids (nothing
  -- inserted, e.g. the whole batch was skipped) makes the guard a no-op.
  IF EXISTS (
    SELECT 1 FROM playlist_tracks pt
    WHERE pt.playlist_id = p_playlist_id
      AND pt.id <> ALL (v_ids)
      AND pt.order_key >= (
        SELECT MIN(order_key) FROM playlist_tracks
        WHERE id = ANY (v_ids)
      )
  ) THEN
    RAISE EXCEPTION 'one of the order_keys in % is not strictly after every other row', p_order_keys
      USING ERRCODE = 'unique_violation', CONSTRAINT = 'ux_playlist_order_key';
  END IF;

  RETURN json_build_object(
    'ok', true,
    'added', v_added,
    'skipped', v_unique_requested - v_added
  );

EXCEPTION
  WHEN unique_violation THEN
    GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
    IF v_constraint = 'ux_playlist_order_key' THEN
      RETURN json_build_object('ok', false, 'error', 'order_key_conflict');
    END IF;
    RAISE;
END;
$function$;

CREATE OR REPLACE FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer, p_order_key text)
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
  v_constraint text;
  v_moved_id uuid;
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

  -- Count tracks
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
      SELECT id AS "internalId", track_id AS "trackId",
        ROW_NUMBER() OVER (ORDER BY order_key) AS position
      FROM playlist_tracks
      WHERE playlist_id = p_playlist_id
    ) t;

    RETURN json_build_object('ok', true, 'noop', true, 'order', v_rows);
  END IF;

  -- The row at p_old_index in the current order_key order: the only
  -- playlist_tracks row this function writes
  SELECT t.id INTO v_moved_id
  FROM (
    SELECT id, ROW_NUMBER() OVER (ORDER BY order_key) AS rn
    FROM playlist_tracks
    WHERE playlist_id = p_playlist_id
  ) t
  WHERE t.rn = p_old_index;

  UPDATE playlist_tracks
  SET order_key = p_order_key
  WHERE id = v_moved_id;

  -- the caller read its neighbours outside this lock: confirm the key it
  -- computed lands the moved row exactly at p_new_index, i.e. exactly
  -- p_new_index - 1 other rows sort before it. A key equal to another
  -- row's has already failed the UPDATE above on ux_playlist_order_key.
  IF (
    SELECT COUNT(*) FROM playlist_tracks
    WHERE playlist_id = p_playlist_id
      AND id <> v_moved_id
      AND order_key < p_order_key
  ) <> p_new_index - 1 THEN
    RAISE EXCEPTION 'order_key % does not land the moved row at index %', p_order_key, p_new_index
      USING ERRCODE = 'unique_violation', CONSTRAINT = 'ux_playlist_order_key';
  END IF;

  -- Bump the parent's updated_at: this happens inside the SAME RPC, so it is
  -- atomic with the reorder. If the reorder commits, this commits; if it fails, so does this.
  UPDATE playlists SET updated_at = now() WHERE id = p_playlist_id;

  -- Return the final order
  SELECT json_agg(row_to_json(t) ORDER BY t.position)
  INTO v_rows
  FROM (
    SELECT id AS "internalId", track_id AS "trackId",
      ROW_NUMBER() OVER (ORDER BY order_key) AS position
    FROM playlist_tracks
    WHERE playlist_id = p_playlist_id
  ) t;

  RETURN json_build_object('ok', true, 'order', v_rows);

EXCEPTION
  WHEN unique_violation THEN
    GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
    IF v_constraint = 'ux_playlist_order_key' THEN
      RETURN json_build_object('ok', false, 'error', 'order_key_conflict');
    END IF;
    RETURN json_build_object('ok', false, 'error', SQLERRM);
  WHEN OTHERS THEN
    RETURN json_build_object('ok', false, 'error', SQLERRM);
END;
$function$;

COMMIT;
