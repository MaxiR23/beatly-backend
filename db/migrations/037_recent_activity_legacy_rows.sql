-- Issue #168. One-time cleanup of the recent_activity rows written before
-- the fixed shape (title on every row, kind on every playlist row).
--
-- The repo owner decided not to guess: this file converts only what it can
-- resolve and deletes every row that still does not fit the shape.
--
-- Order:
--   1. UPDATE: a playlist row without a valid kind (absent, null or any
--      other value than 'user', 'genre', 'liked') gets one when it can be
--      resolved without guessing:
--        entity_id = 'liked'                        -> 'liked'
--        entity_id matches genre_playlists.id       -> 'genre'
--        entity_id matches playlists.id             -> 'user'
--      There is no default branch: a row that matches none of the three is
--      left as it is for the DELETE. The match with playlists is by the
--      existence of the id, without looking at owner_id, like POST /recents;
--      a playlist that was already deleted does not match and is deleted.
--      The order is 'liked' > 'genre' > 'user': an id present in both tables
--      is 'genre' because of the order of the CASE.
--   2. DELETE: every row still outside the shape. That is a playlist row
--      without a valid kind, any row whose title is not a JSON string with
--      at least one non-space character (null, absent, empty, blank or
--      numeric), and any row whose metadata is not a JSON object.
--
-- Depends on 036 being applied first: a row that still has display_name has
-- no title and this file would delete it instead of letting 036 convert it.
-- A new database built from 017 runs the numbers in order.
--
-- The matches in the UPDATE cast the uuid to text (gp.id::text =
-- ra.entity_id and p.id::text = ra.entity_id) and never the text to uuid: entity_id is free text and a
-- value that is not a uuid must not break the statement.
--
-- The kind predicate is wrapped in COALESCE: metadata ->> 'kind' IN (...) is
-- NULL when the key is missing and NOT NULL is NULL, so without it the rows
-- with no kind would not be deleted.
--
-- The UPDATE requires jsonb_typeof = 'object': on an array, || would append
-- an element instead of adding a key. Those rows go to the DELETE.
-- metadata is NOT NULL, so jsonb_typeof is never SQL NULL; a JSON null gives
-- 'null' and is deleted. -> and ->> on a non-object return NULL, no error.
-- The title rule is the one of RecentMetadata.title (str, strip,
-- min_length=1); Postgres [:space:] and Python str.strip() may differ on
-- exotic Unicode spaces, which only affects old rows.
--
-- This also deletes the rows 036 left on purpose (artist_name_only and
-- empty_title_after): they have no valid title.
--
-- Idempotent: after the first run every playlist row has a valid kind or is
-- gone, and every row has a valid title or is gone, so both statements
-- affect 0 rows. No DDL, no policies, no guards. played_at is not named and
-- the table has no triggers, so it does not move. No table references
-- recent_activity, so the DELETE cascades to nothing. The DELETE is
-- irreversible.

BEGIN;

UPDATE public.recent_activity ra
SET metadata = ra.metadata || jsonb_build_object(
    'kind',
    CASE
        WHEN ra.entity_id = 'liked' THEN 'liked'
        WHEN EXISTS (
            SELECT 1 FROM public.genre_playlists gp
            WHERE gp.id::text = ra.entity_id
        ) THEN 'genre'
        WHEN EXISTS (
            SELECT 1 FROM public.playlists p
            WHERE p.id::text = ra.entity_id
        ) THEN 'user'
    END
)
WHERE ra.entity_type = 'playlist'
  AND jsonb_typeof(ra.metadata) = 'object'
  AND NOT COALESCE(ra.metadata ->> 'kind' IN ('user', 'genre', 'liked'), false)
  AND (
      ra.entity_id = 'liked'
      OR EXISTS (
          SELECT 1 FROM public.genre_playlists gp
          WHERE gp.id::text = ra.entity_id
      )
      OR EXISTS (
          SELECT 1 FROM public.playlists p
          WHERE p.id::text = ra.entity_id
      )
  );

DELETE FROM public.recent_activity ra
WHERE jsonb_typeof(ra.metadata) <> 'object'
   OR jsonb_typeof(ra.metadata -> 'title') IS DISTINCT FROM 'string'
   OR ra.metadata ->> 'title' !~ '[^[:space:]]'
   OR (
       ra.entity_type = 'playlist'
       AND NOT COALESCE(ra.metadata ->> 'kind' IN ('user', 'genre', 'liked'), false)
   );

COMMIT;
