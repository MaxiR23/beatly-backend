-- 040: add app_version and a (user_id, created_at) index to error_logs (#191)
--
-- Adds public.error_logs.app_version, a nullable text column, plus
-- idx_error_logs_user_created over (user_id, created_at DESC). This is a
-- new file, not an edit to 017 or any other applied migration: the column
-- does not exist in 017 (lines 1161-1196) nor in any file from 018 to 039.
--
-- The index serves the two queries of services/error_log_service.py (the
-- dedup and the per-user cap), which filter by user_id and a range of
-- created_at. None of the five indexes 017 declares on error_logs includes
-- user_id.
--
-- No guards (no IF NOT EXISTS, no CREATE INDEX CONCURRENTLY): drift
-- against what this file expects must fail loudly, same reasoning as
-- 024-034. BEGIN/COMMIT: the column and its index land together or not at
-- all. No GRANT: the new column is covered by the table's existing grants,
-- and an index needs none. Locks: ADD COLUMN takes ACCESS EXCLUSIVE on
-- error_logs until COMMIT, and the index is built inside that same window.
--
-- Does not touch client_version or any other column of error_logs.
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 039.

BEGIN;

ALTER TABLE public.error_logs
  ADD COLUMN app_version text;

CREATE INDEX idx_error_logs_user_created
  ON public.error_logs USING btree (user_id, created_at DESC);

COMMIT;
