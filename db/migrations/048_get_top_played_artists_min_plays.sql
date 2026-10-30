-- 048: get_top_played_artists also takes p_min_plays (#221, ADR 025)
--
-- Replaces public.get_top_played_artists so it leaves out the artists with
-- fewer than p_min_plays plays (HAVING count(*) >= p_min_plays, the same
-- parameter as get_listen_again and get_replay_songs in 017). The backend
-- passes 1 for the tops and the exclusion threshold
-- (RECOMMENDATIONS_EXCLUDE_MIN_PLAYS) for the artists it never recommends.
-- No DEFAULT: the backend is its only caller, and a caller that forgets it
-- fails (PGRST202) instead of getting a top without the threshold.
--
-- The definition it replaces is the one in 047, the only one there is. The
-- body is the same except for the added parameter and the HAVING.
--
-- DROP + CREATE and not CREATE OR REPLACE: a different argument list is a
-- new function to Postgres, so CREATE OR REPLACE would leave the
-- three-argument one next to it as an overload. The precedents are 028 and
-- 044. A DROP without CASCADE fails loudly if something depends on the
-- function, which is what is wanted.
--
-- DROP + CREATE resets the ACL, and the default privileges of 017 (lines
-- 3110-3113 and 3120-3123) grant EXECUTE to anon and authenticated on a new
-- function, so the three REVOKE lines of 047 are repeated with the new
-- signature. service_role keeps the EXECUTE of the defaults, as in 047. Same
-- warning as 025, 031, 032, 042, 043, 044 and 047.
--
-- One transaction: the DROP, the CREATE and the REVOKEs commit together, so
-- there is no instant without the function nor one where anon and
-- authenticated can call the new one.
--
-- get_top_played_albums, count_plays_since, replace_user_recommendations
-- and the table are not touched.
--
-- SECURITY INVOKER (the default), SET search_path TO 'public' in the bare
-- form, as 047. No guards. Normal migration: it applies to the live
-- database and also runs when building a new database, after 047. Apply it
-- BEFORE deploying the code that passes p_min_plays.

BEGIN;

DROP FUNCTION public.get_top_played_artists(uuid, timestamp with time zone, integer);

CREATE FUNCTION public.get_top_played_artists(
  p_user_id uuid,
  p_since timestamp with time zone,
  p_min_plays integer,
  p_limit integer
)
 RETURNS TABLE(artist_id text, play_count bigint)
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  SELECT
    pe.metadata->'artists'->0->>'id' AS artist_id,
    count(*) AS play_count
  FROM public.play_events pe
  WHERE pe.user_id = p_user_id
    AND pe.played_at >= p_since
    AND coalesce(pe.metadata->'artists'->0->>'id', '') <> ''
  GROUP BY pe.metadata->'artists'->0->>'id'
  HAVING count(*) >= p_min_plays
  ORDER BY count(*) DESC, max(pe.played_at) DESC, pe.metadata->'artists'->0->>'id'
  LIMIT p_limit;
$function$;

REVOKE ALL ON FUNCTION public.get_top_played_artists(uuid, timestamp with time zone, integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_top_played_artists(uuid, timestamp with time zone, integer, integer) FROM anon;
REVOKE ALL ON FUNCTION public.get_top_played_artists(uuid, timestamp with time zone, integer, integer) FROM authenticated;

COMMIT;
