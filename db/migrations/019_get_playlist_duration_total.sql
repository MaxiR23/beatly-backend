-- 019: get_playlist_duration_total RPC for GET /playlists/{playlist_id} (#94)
-- The endpoint needs the sum of duration_seconds across every track in a
-- playlist, not just the ones _list_playlist_tracks() reads: that function
-- caps its read at _TRACKS_LIMIT (services/playlist_service.py), and the
-- whole point of this total is that it does not depend on the cap. Summing
-- in Python would mean dropping that cap for this one number, which
-- defeats it. This is a plain read, not a write: it does not take the
-- parent playlist lock and does not answer through the {ok: ...} envelope
-- the write protocol (013) uses -- it returns the bare number, like
-- get_owned_playlists_with_track (004) already does for its own read.
--
-- No existence or permission check inside: _get_editable_playlist() has
-- already run by the time services/playlist_service.py calls this, and a
-- playlist_id that does not exist (or never did) simply joins to zero rows,
-- which the COALESCE below turns into 0 -- not an error, and not something
-- this function needs to special-case.
--
-- RETURNS bigint, not integer: SUM() over an integer column is bigint in
-- PostgreSQL, and a ::integer cast back would only add a failure mode
-- (integer out of range on a hypothetically huge total) for no benefit --
-- nothing downstream needs the narrower type.
--
-- COALESCE(SUM(...), 0): SUM() over zero rows is NULL, and the empty
-- playlist has to answer 0, not null. Producing that 0 here means Python
-- never has to invent one -- an unexpected null response is treated as an
-- upstream anomaly, not silently defaulted.
--
-- No SECURITY DEFINER (default is INVOKER): this backend only ever calls
-- through the service-role client, which already bypasses RLS, so there is
-- nothing to elevate. Leaving it INVOKER keeps a direct authenticated
-- caller bound by the "playlist_tracks readable by playlist visibility"
-- policy, instead of reopening the kind of hole 018 had to close on
-- move_playlist_track for a function that does not need to be DEFINER in
-- the first place.
--
-- SET search_path TO 'public', matching the bare form used by is_admin,
-- handle_new_user, prevent_role_self_update, is_developer_or_higher and
-- is_tester_or_higher (017) -- not the 'public', 'pg_temp' form 018 needed
-- because it created a temp table. This function creates none, and both
-- relations it touches are schema-qualified, so nothing can shadow them.

CREATE OR REPLACE FUNCTION public.get_playlist_duration_total(p_playlist_id uuid)
 RETURNS bigint
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT COALESCE(SUM(t.duration_seconds), 0)
  FROM public.playlist_tracks pt
  JOIN public.tracks t ON t.id = pt.track_id
  WHERE pt.playlist_id = p_playlist_id;
$function$;
