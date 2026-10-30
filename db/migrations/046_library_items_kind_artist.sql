-- 046: allow kind 'artist' in library_items
--
-- Replaces the CHECK public.library_items.library_items_kind_check, which
-- admits 'album' and 'playlist', with one that also admits 'artist'.
-- This is a new file, not an edit to 017: the CHECK is declared in 017
-- (line 1294) and no file from 018 to 045 touches it (grep of
-- library_items_kind_check).
--
-- It is the database side of LibraryItemKind (models/library.py): the model
-- lets 'artist' through, and without this CHECK a POST /library with
-- kind 'artist' would be a 502.
--
-- No guards, same reasoning as 024-045: if the live CHECK is not the one
-- 017 declares, the DROP CONSTRAINT or the pre-check in the README must
-- fail loudly.
--
-- BEGIN/COMMIT. Lock: ACCESS EXCLUSIVE on library_items while the new CHECK
-- is validated, which scans the table. Brief, but it is the users' library.
-- Not NOT VALID: every existing row is 'album' or 'playlist', so the scan
-- passes.
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 045.
-- Deployment order: apply it BEFORE the code.

BEGIN;

ALTER TABLE public.library_items
  DROP CONSTRAINT library_items_kind_check,
  ADD CONSTRAINT library_items_kind_check
  CHECK (kind = ANY (ARRAY['album'::text, 'playlist'::text, 'artist'::text]));

COMMIT;
