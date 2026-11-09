-- 051: get_played_albums, the albums a user played (#235, ADR 025)
--
-- Returns every album the user played since p_since, with ALL the distinct
-- album titles and ALL the distinct artist ids seen in its plays. The backend
-- uses it to never recommend an album the user already played, nor another
-- edition of it (the editions are matched by artist id and base title).
--
-- It lives in the database because PostgREST does not group by a jsonb path,
-- the same argument as get_top_played_albums in 047.
--
-- It reads the keys log_play() writes into play_events.metadata: `album` (the
-- album title), `album_id` and `artists[*].id`. It does not read `album_name`,
-- the broken key that 047 already notes.
--
-- The union of titles and ids has two limits. A play whose tracks only credit
-- guests gives guest ids and not the id of the album owner, so an edition of
-- that album is not detected. And the other way round: a track of Z with the
-- guest X on the album "Y" yields the key (X, "y"), so an album "Y" of X's
-- own with the same base title is left out too. A play with no album title
-- (an empty or missing `album`) excludes only by album id.
--
-- The filter and the order are those of get_top_played_albums in 047, which
-- is not touched: user_id, played_at >= p_since, a non-empty album_id, and
-- plays desc, last play desc, id. p_limit cuts the tail of that order, so
-- the least played albums are the ones lost.
--
-- The count is taken in `ranked`, over `plays` without the LATERAL. Counting
-- after the join with the artists would multiply it by the number of artists
-- of each track and the order would no longer be that of
-- get_top_played_albums. The coalesce to '{}' means titles and artist_ids are
-- never NULL, so the backend validates list[str] strictly and a NULL would be
-- an anomaly, not an empty. ORDER BY inside array_agg(DISTINCT ...) makes the
-- order of the arrays deterministic.
--
-- Privileges, as 047: the default privileges of 017 (lines 3110-3113 and
-- 3120-3123) grant EXECUTE to anon and authenticated on a new function, so the
-- three REVOKE lines are here. service_role keeps the EXECUTE of the
-- defaults. Same warning as 025, 031, 032, 042, 047 and 048: a future
-- DROP + CREATE resets the ACL.
--
-- SECURITY INVOKER (the default). The p_user_id filter is the only barrier
-- because the caller is the service-role client and the id comes from the
-- token. SET search_path TO 'public' in the bare form, as 047.
--
-- No guards: if the name exists it is drift and it has to fail loudly.
-- Normal migration: live and new database, after 050. Apply it BEFORE
-- deploying the code that calls it.

BEGIN;

CREATE FUNCTION public.get_played_albums(
  p_user_id uuid,
  p_since timestamp with time zone,
  p_limit integer
)
 RETURNS TABLE(album_id text, titles text[], artist_ids text[])
 LANGUAGE sql
 STABLE
 SET search_path TO 'public'
AS $function$
  WITH plays AS (
    SELECT pe.metadata, pe.played_at
    FROM public.play_events pe
    WHERE pe.user_id = p_user_id
      AND pe.played_at >= p_since
      AND coalesce(pe.metadata->>'album_id', '') <> ''
  ),
  ranked AS (
    SELECT
      p.metadata->>'album_id' AS album_id,
      count(*) AS play_count,
      max(p.played_at) AS last_played_at,
      array_agg(DISTINCT p.metadata->>'album' ORDER BY p.metadata->>'album')
        FILTER (WHERE coalesce(p.metadata->>'album', '') <> '') AS titles
    FROM plays p
    GROUP BY p.metadata->>'album_id'
    ORDER BY count(*) DESC, max(p.played_at) DESC, p.metadata->>'album_id'
    LIMIT p_limit
  ),
  credited AS (
    SELECT
      p.metadata->>'album_id' AS album_id,
      array_agg(DISTINCT a->>'id' ORDER BY a->>'id') AS artist_ids
    FROM plays p
    CROSS JOIN LATERAL jsonb_array_elements(p.metadata->'artists') AS a
    WHERE coalesce(a->>'id', '') <> ''
    GROUP BY p.metadata->>'album_id'
  )
  SELECT
    r.album_id,
    coalesce(r.titles, '{}'::text[]) AS titles,
    coalesce(c.artist_ids, '{}'::text[]) AS artist_ids
  FROM ranked r
  LEFT JOIN credited c ON c.album_id = r.album_id
  ORDER BY r.play_count DESC, r.last_played_at DESC, r.album_id;
$function$;

REVOKE ALL ON FUNCTION public.get_played_albums(uuid, timestamp with time zone, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.get_played_albums(uuid, timestamp with time zone, integer) FROM anon;
REVOKE ALL ON FUNCTION public.get_played_albums(uuid, timestamp with time zone, integer) FROM authenticated;

COMMIT;
