-- 020: get_liked_tracks_duration_total RPC for GET /playlists/liked (#112)
-- The virtual "liked songs" playlist needs the sum of duration_seconds
-- across every active like of the caller, not just the ones the
-- endpoint's own read into `tracks` returns: that read caps at
-- _TRACKS_LIMIT (services/playlist_service.py), the same cap
-- _list_playlist_tracks() uses, and the whole point of this total is that
-- it does not depend on that cap. Summing in Python would mean dropping
-- the cap for this one number, which defeats it -- the same reasoning as
-- 019's get_playlist_duration_total, applied to a different source table.
--
-- Not a generalization of 019: that function's join and filter are fixed
-- to playlist_tracks/playlist_id, which is not this domain's shape, and
-- 019 is already applied -- an applied migration is never edited. This is
-- a new function for a different source, not a shared one.
--
-- The join is public.user_likes to public.tracks on tracks.track_id =
-- user_likes.track_id, not tracks.id: that is the real foreign key,
-- user_likes_track_id_fkey (017_schema_baseline.sql), which references
-- public.tracks(track_id), a text provider id -- unlike playlist_tracks,
-- whose track_id is the catalog uuid tracks.id.
--
-- Filters user_likes.deleted_at IS NULL: likes are soft-deleted
-- (services/likes_service.py::unlike_track sets deleted_at rather than
-- deleting the row), so an unliked track must not count toward the total.
--
-- RETURNS bigint, not integer: SUM() over an integer column is bigint in
-- PostgreSQL, and a ::integer cast back would only add a failure mode
-- (integer out of range on a hypothetically huge total) for no benefit --
-- nothing downstream needs the narrower type.
--
-- COALESCE(SUM(...), 0): SUM() over zero rows is NULL, and a user with no
-- active likes has to answer 0, not null. Producing that 0 here means
-- Python never has to invent one -- an unexpected null response is
-- treated as an upstream anomaly, not silently defaulted.
--
-- Takes p_user_id explicit rather than reading auth.uid(): this backend
-- only ever calls through the service-role client (core/database.py),
-- where auth.uid() is null -- the same reason get_owned_playlists_with_track
-- (004) and get_playlist_duration_total (019) take their subject as a
-- parameter instead.
--
-- No SECURITY DEFINER (default is INVOKER): the service-role client
-- already bypasses RLS, so there is nothing to elevate, and staying
-- invoker keeps a direct authenticated caller bound by the "user_likes
-- readable by owner" policy, which only lets them see their own likes --
-- the same reasoning 019 gives for get_playlist_duration_total.
--
-- SET search_path TO 'public', matching the bare form used by
-- get_playlist_duration_total (019) and the five 017 SECURITY DEFINER
-- helpers -- not 018's 'public', 'pg_temp' form, which that function
-- needed only because it created a temp table. This function creates
-- none, and both relations it touches are schema-qualified.

CREATE OR REPLACE FUNCTION public.get_liked_tracks_duration_total(p_user_id uuid)
 RETURNS bigint
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT COALESCE(SUM(t.duration_seconds), 0)
  FROM public.user_likes ul
  JOIN public.tracks t ON t.track_id = ul.track_id
  WHERE ul.user_id = p_user_id
    AND ul.deleted_at IS NULL;
$function$;
