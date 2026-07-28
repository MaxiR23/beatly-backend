-- 006: genre domain (curated genre playlists)
-- Exported from Supabase on 2026-07-28 via pg_get_functiondef (verbatim).
-- get_playlist_thumbnails: up to 4 thumbnails per GENRE playlist (reads
--   genre_playlist_tracks; the user-playlist counterpart is
--   get_user_playlist_thumbnails, in 004). Near-identical names, different
--   tables — do not mix them up.
-- update_genre_playlist_track_count: trigger keeping track_count and
--   updated_at of genre_playlists in sync on INSERT/DELETE of its tracks.

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
  ORDER BY gpt.playlist_id, gpt.rn;
$function$;

CREATE OR REPLACE FUNCTION public.update_genre_playlist_track_count()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  IF TG_OP = 'INSERT' THEN
    UPDATE genre_playlists 
    SET track_count = track_count + 1,
        updated_at = now()
    WHERE id = NEW.playlist_id;
  ELSIF TG_OP = 'DELETE' THEN
    UPDATE genre_playlists 
    SET track_count = GREATEST(track_count - 1, 0),
        updated_at = now()
    WHERE id = OLD.playlist_id;
  END IF;
  RETURN NULL;
END;
$function$;