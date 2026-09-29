-- 036: fixed metadata shape for recent_activity (#166)
--
-- Rows written by the previous client keep their metadata as
-- display_name / artist_name / thumbnail_url. POST /recents now writes
-- title / subtitle / thumbnail_url, always the three keys. This file
-- converts the old rows once, so the Home reads a single shape.
--
-- The new jsonb is built from scratch with exactly three keys and every
-- other legacy key is dropped. title and subtitle are copied as jsonb (->)
-- from display_name and artist_name without inventing data: a null or empty
-- value stays null or empty, and a missing artist_name gives a JSON null.
-- thumbnail_url is normalized with the same rule as
-- _normalize_thumbnail_url() in services/activity_service.py: the first
-- =w[0-9]+-h[0-9]+ (ASCII digits) becomes =w512-h512 (regexp_replace without the g
-- flag). The two places have to change together.
--
-- Idempotent through the WHERE: after the first run no row has display_name.
-- Rows that have artist_name and no display_name are NOT touched.
--
-- No DDL, no policies, no guards. This is the first data migration in the
-- repo. played_at is not named by the statement and recent_activity has no
-- triggers, so it does not move.

BEGIN;

UPDATE public.recent_activity
SET metadata = jsonb_build_object(
    'title', metadata -> 'display_name',
    'subtitle', metadata -> 'artist_name',
    'thumbnail_url', regexp_replace(metadata ->> 'thumbnail_url', '=w[0-9]+-h[0-9]+', '=w512-h512')
)
WHERE metadata ? 'display_name';

COMMIT;
