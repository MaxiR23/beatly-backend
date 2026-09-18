-- 023: align the two genre-domain thumbnail RPCs with the empty-string
-- filter get_user_playlist_thumbnails already has (#125)
--
-- Two functions build a mosaic of thumbnails by reading
-- genre_playlist_tracks JOIN tracks (t.track_id = gpt.track_id) and each
-- filters out a track whose thumbnail_url is NULL, but not one whose
-- thumbnail_url is the empty string: get_playlist_thumbnails
-- (006_genre.sql, current body in 017) and recommend_playlists_by_history
-- (008_recommendations_feed.sql, current body in 017; 018-022 do not
-- redefine either). Since public.tracks.thumbnail_url is NOT NULL (017
-- line 1429), the existing IS NOT NULL never drops anything for either
-- one; this file adds AND t.thumbnail_url <> '' to each WHERE, one line,
-- immediately after the existing IS NOT NULL. Nothing else in either
-- body changes. With this applied, the three functions in this repo that
-- filter tracks.thumbnail_url -- these two plus get_user_playlist_thumbnails
-- -- all carry the same two conditions.
--
-- get_user_playlist_thumbnails is NOT touched here: it has had both
-- conditions since 004_playlists.sql. recommend_playlists_by_history has
-- no caller in services/ or routes/ today -- this backend does not expose
-- a recommendations endpoint yet -- so this half of the file is a
-- preventive fix, not a bug fix for any live response.
--
-- New file, not an edit to 006, 008 or 017: all three are already
-- applied, and an applied migration is never edited.
--
-- No GRANT or REVOKE: neither signature changes, so the privileges 017
-- already applied -- lines 2754-2759 for get_playlist_thumbnails, lines
-- 2874-2876 for recommend_playlists_by_history, both to anon,
-- authenticated and service_role -- keep covering the functions without
-- being reapplied.
--
-- Both bodies below are copied verbatim from 017 with \r stripped, the
-- same normalization 021 and 022 already apply to bodies copied out of
-- 017; no literal in either body spans more than one line, so this has
-- no semantic effect. get_playlist_thumbnails keeps its STABLE clause,
-- unchanged. recommend_playlists_by_history carries no volatility
-- clause in 017, which makes it VOLATILE (the default) -- that is
-- preserved here by omitting the clause, not by adding STABLE for
-- symmetry with the other function.

CREATE OR REPLACE FUNCTION public.get_playlist_thumbnails(playlist_ids uuid[], limit_per_playlist integer DEFAULT 4)
 RETURNS TABLE(playlist_id uuid, thumbnail_url text)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT 
    gpt.playlist_id,
    t.thumbnail_url
  FROM (
    SELECT 
      playlist_id,
      track_id,
      ROW_NUMBER() OVER (PARTITION BY playlist_id ORDER BY position) as rn
    FROM genre_playlist_tracks
    WHERE playlist_id = ANY(playlist_ids)
  ) gpt
  JOIN tracks t ON t.track_id = gpt.track_id
  WHERE gpt.rn <= limit_per_playlist
    AND t.thumbnail_url IS NOT NULL
    AND t.thumbnail_url <> ''
  ORDER BY gpt.playlist_id, gpt.rn;
$function$;

CREATE OR REPLACE FUNCTION public.recommend_playlists_by_history(p_user_id uuid, p_days integer DEFAULT 45, p_limit integer DEFAULT 5)
 RETURNS TABLE(playlist_id uuid, playlist_title text, total_tracks integer, matching_tracks bigint, matching_artists bigint, thumbnails jsonb)
 LANGUAGE sql
AS $function$
  WITH top_artists AS (
    SELECT
      (a.value->>'id') AS artist_id,
      COUNT(*) AS play_count
    FROM play_events pe
    CROSS JOIN LATERAL jsonb_array_elements(pe.metadata->'artists') AS a
    WHERE pe.user_id = p_user_id
      AND pe.played_at >= now() - (p_days || ' days')::interval
      AND a.value->>'id' IS NOT NULL
    GROUP BY (a.value->>'id')
    ORDER BY play_count DESC
    LIMIT 20
  ),
  track_artists AS (
    SELECT
      t.track_id,
      (a.value->>'id') AS artist_id
    FROM tracks t
    CROSS JOIN LATERAL jsonb_array_elements(t.artists) AS a
    WHERE a.value->>'id' IS NOT NULL
  ),
  matched_tracks AS (
    SELECT
      gpt.playlist_id,
      ta_track.track_id,
      ta_track.artist_id
    FROM top_artists ta
    JOIN track_artists ta_track ON ta_track.artist_id = ta.artist_id
    JOIN genre_playlist_tracks gpt ON gpt.track_id = ta_track.track_id
  ),
  ranked AS (
    SELECT
      m.playlist_id,
      gp.title AS playlist_title,
      gp.track_count AS total_tracks,
      COUNT(DISTINCT m.track_id) AS matching_tracks,
      COUNT(DISTINCT m.artist_id) AS matching_artists
    FROM matched_tracks m
    JOIN genre_playlists gp ON gp.id = m.playlist_id
    GROUP BY m.playlist_id, gp.title, gp.track_count
    ORDER BY COUNT(DISTINCT m.artist_id) DESC, COUNT(DISTINCT m.track_id) DESC
    LIMIT p_limit
  ),
  thumbs AS (
    SELECT
      gpt.playlist_id,
      jsonb_agg(t.thumbnail_url ORDER BY gpt.position) AS thumbnails
    FROM genre_playlist_tracks gpt
    JOIN tracks t ON t.track_id = gpt.track_id
    WHERE gpt.playlist_id IN (SELECT playlist_id FROM ranked)
      AND gpt.position <= 4
      AND t.thumbnail_url IS NOT NULL
      AND t.thumbnail_url <> ''
    GROUP BY gpt.playlist_id
  )
  SELECT
    r.playlist_id,
    r.playlist_title,
    r.total_tracks,
    r.matching_tracks,
    r.matching_artists,
    COALESCE(th.thumbnails, '[]'::jsonb) AS thumbnails
  FROM ranked r
  LEFT JOIN thumbs th ON th.playlist_id = r.playlist_id;
$function$;
