-- 028: make playlist_tracks.order_key NOT NULL, replace its index with a
-- unique one, and switch add_playlist_track, add_playlist_tracks_bulk and
-- move_playlist_track to write it (#135, stage 2 of 3)
--
-- Requires db/backfills/playlist_tracks_order_key.sql to have just been
-- re-run, with no write to playlist_tracks in between, and its "After
-- running" checks (null_keys = 0, no duplicate key per playlist, same
-- order as position) confirmed -- see db/backfills/README.md. This file
-- does not run that backfill; it only asserts, then requires, that it
-- already ran cleanly.
--
-- Order inside this transaction, and why:
-- 1. A DO block asserts, for every playlist, that the order by order_key
--    matches the order by position -- the same query the backfill's own
--    "After running" check runs (db/backfills/README.md, criterion 4 of
--    #133), pointed at RAISE EXCEPTION instead of a SELECT a human reads.
--    This runs BEFORE the SET NOT NULL below on purpose: a move landed in
--    the window between the backfill run and this file leaves order_key
--    out of sync with position without ever leaving a row NULL, so the
--    NOT NULL check alone would not catch it.
-- 2. ALTER COLUMN order_key SET NOT NULL -- catches the other half of
--    that same window: a row added (not moved) after the backfill's last
--    run is NULL, and this fails loudly on it rather than silently
--    building a unique index over nulls (nulls are never equal to each
--    other, so a unique index would accept any number of them).
-- 3. DROP the old index, idx_playlist_tracks_playlist_order_key
--    (027, non-unique, (playlist_id, order_key, id)) and CREATE UNIQUE
--    INDEX ux_playlist_order_key ON (playlist_id, order_key) in its
--    place -- same prefix as the table's two other unique indexes,
--    ux_playlist_pos and ux_playlist_track (017 lines 1780, 1788). One
--    index covers the same range query the old one did, so keeping both
--    would be redundant. Immediate, not DEFERRABLE, and no CONCURRENTLY
--    or IF EXISTS/IF NOT EXISTS guard: a duplicate or NULL key has to
--    fail the CREATE UNIQUE INDEX statement itself, inside this
--    transaction, not surface later as a deferred-constraint violation
--    the four functions below could never catch by CONSTRAINT_NAME (see
--    the next paragraph), and drift must fail loudly rather than be
--    masked, same reasoning as 024-027.
-- 4. add_playlist_track, add_playlist_tracks_bulk and move_playlist_track
--    each gain an order_key parameter -- adding a parameter is not
--    something CREATE OR REPLACE FUNCTION can do across a different
--    argument list, so each is DROP FUNCTION on the current 3-argument
--    signature (022 for add/move, 017 for bulk) followed by CREATE
--    FUNCTION on the 4-argument one. DROP + CREATE resets privileges:
--    PUBLIC gets its default EXECUTE back, and the default privileges
--    017 sets (line 3111, ALTER DEFAULT PRIVILEGES ... GRANT ALL ON
--    FUNCTIONS TO anon) hand anon an explicit grant back too. For add and
--    bulk the result matches what is already applied (017 lines
--    2667-2678: anon, authenticated and service_role, no REVOKE FROM
--    PUBLIC) -- nothing to redo. move_playlist_track is different: it is
--    REVOKE ALL ... FROM PUBLIC with no GRANT to anon today (017 lines
--    2838-2840, finding 7), and it is SECURITY DEFINER, so this file
--    repeats REVOKE ALL ... FROM PUBLIC and FROM anon on it right after
--    its CREATE, closing the same hole finding 7 already closed once.
--    Each of the three bodies is copied verbatim from its current source
--    except for: the new order_key parameter and column, an order-check
--    guard described below, and (add, bulk) an EXCEPTION branch that
--    recognizes a violation of the new unique index by name. get_
--    user_playlist_thumbnails keeps its 3-argument-equivalent signature
--    (its two arguments are unchanged) and its body verbatim except for
--    ORDER BY position -> ORDER BY order_key inside the ROW_NUMBER()
--    window, so it stays a plain CREATE OR REPLACE and keeps its ACL,
--    same reasoning as 023.
--
-- Collision handling, all three writers: each RPC now receives an
-- order_key the caller (services/playlist_service.py) already computed
-- from a read taken outside this function's lock. Reading, computing and
-- writing is therefore not atomic, so under the lock this file adds a
-- guard, before the success RETURN, that the received key still falls
-- strictly between its real neighbours (add/move: no other row has an
-- order_key on the wrong side, identified by id; bulk: same, but the
-- rows this call wrote are identified by the id array the INSERT's own
-- RETURNING clause captures, not by matching order_key values -- two
-- concurrent writers can compute the same key from the same last-seen
-- key, so an existing row's order_key can equal one of this batch's
-- keys without this batch having written it). A guard that fails raises
-- an exception tagged with the same SQLSTATE and CONSTRAINT name
-- PostgreSQL would use for a real violation of ux_playlist_order_key
-- (RAISE ... USING ERRCODE =
-- 'unique_violation', CONSTRAINT = 'ux_playlist_order_key') so it falls
-- into the exact same EXCEPTION branch a genuine index violation does --
-- one handler covers both a key that collides and one that is merely out
-- of order. That branch answers {"ok": false, "error":
-- "order_key_conflict"} in the RPC's own JSON envelope, the same key
-- (error, not reason) add_playlist_track already uses for
-- track_already_in_playlist -- reason is the HTTP-facing name the
-- service translates this into (Conflict("order_key_conflict") -> 409),
-- not the RPC's own vocabulary. Any other unique_violation, and any
-- other error, keeps following its current path (add: track_
-- already_in_playlist; bulk: RAISE, so it propagates unchanged; move:
-- the existing WHEN OTHERS, returning SQLERRM in error, finding 4). Since
-- an EXCEPTION clause wraps each of these functions in an implicit
-- savepoint, catching either flavor of unique_violation rolls back
-- everything the function wrote before the guard fired -- the response
-- never reports a conflict while also leaving a partial write behind.
--
-- Untouched: position (still assigned in the same statement as before in
-- all three functions, still renumbered by move_playlist_track exactly
-- as today), ux_playlist_pos, playlist_tracks_reorder() (current body in
-- 022) and trg_playlist_tracks_reorder (still disabled live, still not
-- dropped -- that is stage 3, together with position). No routes/,
-- services/ or models/ code outside services/playlist_service.py reads
-- or writes order_key.
--
-- Locks: ALTER COLUMN ... SET NOT NULL and CREATE (UNIQUE) INDEX (no
-- CONCURRENTLY) each take ACCESS EXCLUSIVE on playlist_tracks until
-- COMMIT. This file never touches playlists, so there is no reverse lock
-- order against the write protocol of 013.
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 027.

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
    RAISE EXCEPTION 'playlist_tracks: % row(s) where the order_key order does not match the position order -- re-run db/backfills/playlist_tracks_order_key.sql and confirm its "After running" checks before applying this migration again', v_mismatches;
  END IF;
END;
$$;

ALTER TABLE public.playlist_tracks
  ALTER COLUMN order_key SET NOT NULL;

DROP INDEX public.idx_playlist_tracks_playlist_order_key;

CREATE UNIQUE INDEX ux_playlist_order_key
  ON public.playlist_tracks USING btree (playlist_id, order_key);

DROP FUNCTION public.add_playlist_track(uuid, uuid, uuid);

CREATE FUNCTION public.add_playlist_track(p_playlist_id uuid, p_track_id uuid, p_added_by uuid, p_order_key text)
 RETURNS json
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_next_pos int;
  v_row playlist_tracks%ROWTYPE;
  v_constraint text;
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

  INSERT INTO playlist_tracks (playlist_id, track_id, position, added_by, order_key)
  VALUES (p_playlist_id, p_track_id, v_next_pos, p_added_by, p_order_key)
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

  RETURN json_build_object('ok', true, 'id', v_row.id, 'position', v_row.position);

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

DROP FUNCTION public.add_playlist_tracks_bulk(uuid, uuid[], uuid);

CREATE FUNCTION public.add_playlist_tracks_bulk(p_playlist_id uuid, p_track_ids uuid[], p_added_by uuid, p_order_keys text[])
 RETURNS json
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_base_pos int;
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

  SELECT COALESCE(MAX(position), 0)
  INTO v_base_pos
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

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
    INSERT INTO playlist_tracks (playlist_id, track_id, position, added_by, order_key)
    SELECT p_playlist_id, tid, v_base_pos + rn, p_added_by, okey
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

DROP FUNCTION public.move_playlist_track(uuid, integer, integer);

CREATE FUNCTION public.move_playlist_track(p_playlist_id uuid, p_old_index integer, p_new_index integer, p_order_key text)
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

  -- Defer constraint to allow duplicate temporary positions
  SET CONSTRAINTS ux_playlist_pos DEFERRED;

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
      SELECT id AS "internalId", track_id AS "trackId", position
      FROM playlist_tracks
      WHERE playlist_id = p_playlist_id
      ORDER BY position
    ) t;

    RETURN json_build_object('ok', true, 'noop', true, 'order', v_rows);
  END IF;

  -- Create a temp table with the current order
  CREATE TEMP TABLE tmp_reorder AS
  SELECT id, track_id, ROW_NUMBER() OVER (ORDER BY position) AS rn
  FROM playlist_tracks
  WHERE playlist_id = p_playlist_id;

  -- Move to temporary negative positions (based on the new order)
  IF p_new_index < p_old_index THEN
    -- Move up: the moved row goes to new_index, the rest move down
    UPDATE playlist_tracks pt
    SET position = CASE
      WHEN t.rn = p_old_index THEN -p_new_index
      WHEN t.rn >= p_new_index AND t.rn < p_old_index THEN -(t.rn + 1)
      ELSE -t.rn
    END
    FROM tmp_reorder t
    WHERE pt.id = t.id;
  ELSE
    -- Move down: the moved row goes to new_index, the rest move up
    UPDATE playlist_tracks pt
    SET position = CASE
      WHEN t.rn = p_old_index THEN -p_new_index
      WHEN t.rn > p_old_index AND t.rn <= p_new_index THEN -(t.rn - 1)
      ELSE -t.rn
    END
    FROM tmp_reorder t
    WHERE pt.id = t.id;
  END IF;

  -- Convert negative positions back to positive
  UPDATE playlist_tracks
  SET position = -position
  WHERE playlist_id = p_playlist_id AND position < 0;

  -- Write the moved row's order key: the only row whose order_key changes
  UPDATE playlist_tracks pt
  SET order_key = p_order_key
  FROM tmp_reorder t
  WHERE pt.id = t.id AND t.rn = p_old_index
  RETURNING pt.id INTO v_moved_id;

  -- the caller read its neighbours outside this lock: confirm the key it
  -- computed still lands strictly between the moved row's real neighbours
  IF EXISTS (
    SELECT 1 FROM playlist_tracks
    WHERE playlist_id = p_playlist_id
      AND id <> v_moved_id
      AND ((position < p_new_index AND order_key >= p_order_key)
        OR (position > p_new_index AND order_key <= p_order_key))
  ) THEN
    RAISE EXCEPTION 'order_key % is not strictly between the neighbours of position %', p_order_key, p_new_index
      USING ERRCODE = 'unique_violation', CONSTRAINT = 'ux_playlist_order_key';
  END IF;

  -- Clean up
  DROP TABLE tmp_reorder;

  -- Bump the parent's updated_at: this happens inside the SAME RPC, so it is
  -- atomic with the reorder. If the reorder commits, this commits; if it fails, so does this.
  UPDATE playlists SET updated_at = now() WHERE id = p_playlist_id;

  -- Return the final order
  SELECT json_agg(row_to_json(t) ORDER BY t.position)
  INTO v_rows
  FROM (
    SELECT id AS "internalId", track_id AS "trackId", position
    FROM playlist_tracks
    WHERE playlist_id = p_playlist_id
    ORDER BY position
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

REVOKE ALL ON FUNCTION public.move_playlist_track(uuid, integer, integer, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.move_playlist_track(uuid, integer, integer, text) FROM anon;

CREATE OR REPLACE FUNCTION public.get_user_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer DEFAULT 4) RETURNS TABLE(playlist_id uuid, thumbnail_url text)
 LANGUAGE sql STABLE
AS $function$
  SELECT
    upt.playlist_id,
    t.thumbnail_url
  FROM (
    SELECT
      playlist_id,
      track_id,
      ROW_NUMBER() OVER (PARTITION BY playlist_id ORDER BY order_key) AS rn
    FROM playlist_tracks
    WHERE playlist_id = ANY(playlist_ids)
  ) upt
  JOIN tracks t ON t.id = upt.track_id
  WHERE upt.rn <= limit_per_playlist
    AND t.thumbnail_url IS NOT NULL
    AND t.thumbnail_url <> ''
  ORDER BY upt.playlist_id, upt.rn;
$function$;

COMMIT;
