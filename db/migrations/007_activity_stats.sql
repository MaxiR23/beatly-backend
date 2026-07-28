-- 007: activity (play_events) and weekly stats
-- Exported from Supabase on 2026-07-28 via pg_get_functiondef (verbatim).
-- aggregate_user_weekly_stats: a user's top 10 tracks/artists/albums for a
--   week, from play_events.metadata. Used by the stats pipeline (stats
--   domain: out of V1 in the new backend).
-- get_active_users_in_period / get_users_with_weekly_stats: helpers for
--   the same pipeline (SECURITY DEFINER).
-- purge_old_data: play_events maintenance (~450k row ceiling, prunes down
--   to ~360k). Confirm how it is invoked (likely a Supabase cron).

CREATE OR REPLACE FUNCTION public.aggregate_user_weekly_stats(p_user_id uuid, p_week_start date, p_week_end date)
 RETURNS jsonb
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_tracks jsonb;
  v_artists jsonb;
  v_albums jsonb;
BEGIN
  -- Top 10 tracks
  SELECT COALESCE(jsonb_agg(t ORDER BY t.play_count DESC), '[]'::jsonb)
  INTO v_tracks
  FROM (
    SELECT 
      track_id as entity_id,
      (array_agg(metadata->>'track_name') FILTER (WHERE metadata->>'track_name' IS NOT NULL))[1] as display_name,
      (array_agg(metadata->>'thumbnail_url') FILTER (WHERE metadata->>'thumbnail_url' IS NOT NULL))[1] as thumbnail_url,
      (array_agg(metadata->'artists') FILTER (WHERE metadata->'artists' IS NOT NULL))[1] as artists,
      (array_agg(metadata->>'album_id') FILTER (WHERE metadata->>'album_id' IS NOT NULL))[1] as album_id,
      (array_agg(metadata->>'album_name') FILTER (WHERE metadata->>'album_name' IS NOT NULL))[1] as album_name,
      (array_agg((metadata->>'duration_seconds')::int) FILTER (WHERE metadata->>'duration_seconds' IS NOT NULL))[1] as duration_seconds,
      COUNT(*)::int as play_count
    FROM play_events
    WHERE user_id = p_user_id 
      AND played_at >= p_week_start 
      AND played_at < p_week_end
    GROUP BY track_id
    ORDER BY play_count DESC
    LIMIT 10
  ) t;

  -- Top 10 artists (desde el array metadata->'artists', SIN fallback)
  SELECT COALESCE(jsonb_agg(a ORDER BY a.play_count DESC), '[]'::jsonb)
  INTO v_artists
  FROM (
    SELECT 
      artist_elem->>'id' as entity_id,
      (array_agg(artist_elem->>'name') FILTER (WHERE artist_elem->>'name' IS NOT NULL))[1] as display_name,
      (array_agg(pe.metadata->>'thumbnail_url') FILTER (WHERE pe.metadata->>'thumbnail_url' IS NOT NULL))[1] as thumbnail_url,
      COUNT(*)::int as play_count
    FROM play_events pe,
      LATERAL jsonb_array_elements(pe.metadata->'artists') as artist_elem
    WHERE pe.user_id = p_user_id 
      AND pe.played_at >= p_week_start 
      AND pe.played_at < p_week_end
      AND artist_elem->>'id' IS NOT NULL
    GROUP BY artist_elem->>'id'
    ORDER BY play_count DESC
    LIMIT 10
  ) a;

  -- Top 10 albums (artist_id/artist_name desde el primer elemento del array)
  SELECT COALESCE(jsonb_agg(al ORDER BY al.play_count DESC), '[]'::jsonb)
  INTO v_albums
  FROM (
    SELECT 
      metadata->>'album_id' as entity_id,
      (array_agg(metadata->>'album_name') FILTER (WHERE metadata->>'album_name' IS NOT NULL))[1] as display_name,
      (array_agg(metadata->>'thumbnail_url') FILTER (WHERE metadata->>'thumbnail_url' IS NOT NULL))[1] as thumbnail_url,
      (array_agg(metadata->'artists'->0->>'id') FILTER (WHERE metadata->'artists'->0->>'id' IS NOT NULL))[1] as artist_id,
      (array_agg(metadata->'artists'->0->>'name') FILTER (WHERE metadata->'artists'->0->>'name' IS NOT NULL))[1] as artist_name,
      COUNT(*)::int as play_count
    FROM play_events
    WHERE user_id = p_user_id 
      AND played_at >= p_week_start 
      AND played_at < p_week_end
      AND metadata->>'album_id' IS NOT NULL
    GROUP BY metadata->>'album_id'
    ORDER BY play_count DESC
    LIMIT 10
  ) al;

  RETURN jsonb_build_object(
    'track', v_tracks,
    'artist', v_artists,
    'album', v_albums
  );
END;
$function$;

CREATE OR REPLACE FUNCTION public.get_active_users_in_period(p_start date, p_end date)
 RETURNS TABLE(user_id uuid)
 LANGUAGE sql
 SECURITY DEFINER
AS $function$
  SELECT DISTINCT pe.user_id
  FROM play_events pe
  WHERE pe.played_at >= p_start 
    AND pe.played_at < p_end;
$function$;

CREATE OR REPLACE FUNCTION public.get_users_with_weekly_stats(p_weeks date[])
 RETURNS TABLE(user_id uuid)
 LANGUAGE sql
 SECURITY DEFINER
AS $function$
  SELECT DISTINCT uws.user_id
  FROM user_weekly_stats uws
  WHERE uws.week_start = ANY(p_weeks);
$function$;

CREATE OR REPLACE FUNCTION public.purge_old_data()
 RETURNS void
 LANGUAGE plpgsql
AS $function$
declare
  play_count int;
  cutoff_date timestamptz;
begin
  select count(*) into play_count from play_events;

  if play_count > 450000 then
    select played_at into cutoff_date
    from play_events
    order by played_at desc
    offset 360000 limit 1;

    delete from play_events where played_at < cutoff_date;
  end if;
end;
$function$;