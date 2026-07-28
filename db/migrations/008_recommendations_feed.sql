-- 008: recommendations / feed (read play_events and catalogs)
-- Exported from Supabase on 2026-07-28 via pg_get_functiondef (verbatim).
-- All of these feed domains outside V1 of the new backend (feed, featured,
-- station, etc.). Versioned anyway: the new database needs them.
-- get_featured_new_release: recent release by the user's most played
--   artist (90 days).
-- get_listen_again: album with >= p_min_plays not played for
--   p_not_played_days.
-- get_replay_songs: top tracks over 60 days with exponential decay.
-- recommend_playlists_by_history: genre playlists ranked by match against
--   the user's most played artists.

CREATE OR REPLACE FUNCTION public.get_featured_new_release(p_user_id uuid)
 RETURNS TABLE(album_id text, album_name text, artist_id text, artist_name text, release_date date, thumbnail_url text, track_count integer, play_count bigint)
 LANGUAGE sql
 STABLE
AS $function$
  WITH user_artist_plays AS (
    SELECT
      COALESCE(metadata->'artists'->0->>'id', '')   AS pe_artist_id,
      COALESCE(metadata->'artists'->0->>'name', '') AS pe_artist_name,
      COUNT(*)                                       AS play_count
    FROM play_events
    WHERE user_id = p_user_id
      AND played_at >= NOW() - INTERVAL '90 days'
    GROUP BY pe_artist_id, pe_artist_name
  )
  SELECT
    ur.album_id,
    ur.album_name,
    ur.artist_id,
    ur.artist_name,
    ur.release_date,
    ur.thumbnail_url,
    ur.track_count,
    uap.play_count
  FROM upcoming_releases ur
  INNER JOIN user_artist_plays uap
    ON (
      ur.artist_id IS NOT NULL
      AND uap.pe_artist_id != ''
      AND ur.artist_id = uap.pe_artist_id
    )
    OR (
      (ur.artist_id IS NULL OR uap.pe_artist_id = '')
      AND LOWER(ur.artist_name) = LOWER(uap.pe_artist_name)
    )
  WHERE ur.status = 'released'
    AND ur.release_date >= CURRENT_DATE - 45
  ORDER BY uap.play_count DESC, ur.release_date DESC
  LIMIT 1;
$function$;

CREATE OR REPLACE FUNCTION public.get_listen_again(p_user_id uuid, p_min_plays integer DEFAULT 3, p_not_played_days integer DEFAULT 7, p_limit integer DEFAULT 1)
 RETURNS TABLE(album_id text, album_name text, artist_id text, artist_name text, thumbnail_url text, total_plays bigint, last_played_at timestamp with time zone)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT
    metadata->>'album_id'                       AS album_id,
    metadata->>'album_name'                     AS album_name,
    metadata->'artists'->0->>'id'               AS artist_id,
    metadata->'artists'->0->>'name'             AS artist_name,
    metadata->>'thumbnail_url'                  AS thumbnail_url,
    count(*)                                    AS total_plays,
    max(played_at)                              AS last_played_at
  FROM play_events
  WHERE user_id = p_user_id
    AND metadata->>'album_id' IS NOT NULL
  GROUP BY
    metadata->>'album_id',
    metadata->>'album_name',
    metadata->'artists'->0->>'id',
    metadata->'artists'->0->>'name',
    metadata->>'thumbnail_url'
  HAVING
    count(*) >= p_min_plays
    AND max(played_at) < now() - make_interval(days => p_not_played_days)
  ORDER BY count(*) DESC
  LIMIT p_limit;
$function$;

CREATE OR REPLACE FUNCTION public.get_replay_songs(p_user_id uuid, p_min_plays integer DEFAULT 2, p_limit integer DEFAULT 30)
 RETURNS TABLE(track_id text, track_name text, artists jsonb, album_name text, album_id text, thumbnail_url text, duration_seconds integer, play_count bigint)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT
    pe.track_id,
    MAX(pe.metadata->>'track_name')              AS track_name,
    (array_agg(pe.metadata->'artists') FILTER (WHERE pe.metadata->'artists' IS NOT NULL))[1] AS artists,
    MAX(pe.metadata->>'album_name')              AS album_name,
    MAX(pe.metadata->>'album_id')                AS album_id,
    MAX(pe.metadata->>'thumbnail_url')           AS thumbnail_url,
    MAX((pe.metadata->>'duration_seconds')::INT) AS duration_seconds,
    COUNT(*)                                     AS play_count
  FROM play_events pe
  WHERE
    pe.user_id = p_user_id
    AND pe.played_at >= NOW() - INTERVAL '60 days'
  GROUP BY pe.track_id
  HAVING COUNT(*) >= p_min_plays
  ORDER BY SUM(EXP(-EXTRACT(EPOCH FROM (NOW() - pe.played_at)) / (86400 * 20))) DESC
  LIMIT p_limit;
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