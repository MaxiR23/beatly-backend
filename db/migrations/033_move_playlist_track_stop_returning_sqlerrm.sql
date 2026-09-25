-- 033: move_playlist_track stops returning SQLERRM in its own JSON (#148)
--
-- move_playlist_track's EXCEPTION block (current body since 029) has two
-- branches -- WHEN unique_violation, when the constraint that fired is
-- not ux_playlist_order_key, and WHEN OTHERS -- that both RETURN
-- json_build_object('ok', false, 'error', SQLERRM), the raw text of the
-- Postgres error. services/playlist_service.py's _rpc_result() already
-- turns any ok: false other than order_key_conflict into a generic
-- UpstreamError (502 upstream_error) without reading the error field, so
-- no shipped HTTP client ever sees that text -- but a caller that invokes
-- the RPC directly (its own JWT, the SQL editor, a future service_role
-- caller) sees the raw Postgres error in the RPC's own JSON envelope.
-- 030, 031 and 032 do not touch this function; 029 (lines 258-368) is
-- still its current body, redefined here with CREATE OR REPLACE FUNCTION.
--
-- Both branches now return the fixed reason 'internal_error' instead:
-- the default reason of AppError (core/exceptions.py), already documented
-- in docs/api/conventions.md, and it does not collide with the function's
-- own four reasons (playlist_not_found, forbidden, empty_playlist,
-- order_key_conflict) -- none of which is internal_error. The backend does
-- not need to change: it never read the discarded text either.
--
-- The issue requires the detail to still reach the database log, as it
-- does everywhere else a Postgres error is not swallowed -- so each
-- branch gains a RAISE LOG statement, with the error message, SQLSTATE
-- and p_playlist_id (and, in the unique_violation branch, the offending
-- constraint name) before its RETURN. RAISE LOG, not RAISE WARNING:
-- with Postgres's default GUCs (client_min_messages = notice,
-- log_min_messages = warning), a LOG message is written to the server
-- log and never sent to the client, while a WARNING is written to the
-- log AND travels to the client as a notice -- which would reopen, on a
-- different channel, the same leak this file closes.
--
-- CREATE OR REPLACE FUNCTION, not DROP + CREATE: the signature does not
-- change, so it keeps every grant and revoke already applied (anon = f,
-- authenticated = t, service_role = t, PUBLIC = f, the state 028 left) --
-- no GRANT or REVOKE needed here. CREATE OR REPLACE resets anything not
-- restated, so SECURITY DEFINER and SET search_path TO 'public',
-- 'pg_temp' are copied verbatim below, same warning as 029's own header
-- (lines 41-46) -- omitting either would silently drop the function to
-- SECURITY INVOKER or an unpinned search_path with no error at apply
-- time. BEGIN/COMMIT, same as every file that has changed a function on
-- the live database since 025.
--
-- The rest of the body below is verbatim from 029 (lines 258-363, up to
-- and including the unique_violation branch's "END IF;"), unchanged
-- except for the two EXCEPTION branches noted above. The README entry
-- for this file carries the byte-for-byte diff that proves it.

BEGIN;

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
    RAISE LOG 'move_playlist_track: unique_violation on constraint % for playlist %: % (SQLSTATE %)', v_constraint, p_playlist_id, SQLERRM, SQLSTATE;
    RETURN json_build_object('ok', false, 'error', 'internal_error');
  WHEN OTHERS THEN
    RAISE LOG 'move_playlist_track: unhandled error for playlist %: % (SQLSTATE %)', p_playlist_id, SQLERRM, SQLSTATE;
    RETURN json_build_object('ok', false, 'error', 'internal_error');
END;
$function$;

COMMIT;
