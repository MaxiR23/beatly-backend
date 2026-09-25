-- 032: revoke EXECUTE from authenticated on get_active_users_in_period and
-- get_users_with_weekly_stats, leaving service_role (#146)
--
-- Both functions are SECURITY DEFINER (017 lines 375-376 and 573-574,
-- vigente 021), so they run as postgres, read play_events / user_weekly_stats
-- without filtering by caller, and return user ids for the whole period.
-- Any authenticated caller could use them to bypass the per-owner policies
-- on those two tables.
--
-- Their ACL in 017 (lines 2721-2723 and 2784-2786) already does
-- REVOKE ALL ... FROM PUBLIC, with GRANT ALL only to authenticated and to
-- service_role -- no GRANT ... TO anon. So, unlike the five functions 025
-- closed, this file needs only one REVOKE line per function
-- (FROM authenticated); PUBLIC and anon are not touched.
--
-- Nobody calls them: not the backend (routes/, services/, zero hits), not
-- another function, policy or view in public per 017, and not the one live
-- cron job (purge-old-data -> purge_old_data()). db/migrations/README.md
-- has the queries to confirm this live before applying.
--
-- No GRANT: service_role keeps the grant it already has from 017. No
-- CREATE OR REPLACE: neither body, signature, volatility, SECURITY
-- DEFINER nor search_path changes for either function.
--
-- Same warning as 025/031: a future DROP + CREATE FUNCTION on either of
-- these two resets its ACL, and the default privileges 017 sets (lines
-- 3111-3112, GRANT ALL ON FUNCTIONS TO anon and TO authenticated) hand
-- EXECUTE back to both anon and authenticated. Both the REVOKE ... FROM
-- PUBLIC from 017 and the REVOKE below would need to be repeated.
-- CREATE OR REPLACE FUNCTION resets neither.
--
-- BEGIN/COMMIT, same as every file that has changed privileges on the
-- live database since 025: both functions end up revoked, or neither does.

BEGIN;

REVOKE ALL ON FUNCTION public.get_active_users_in_period(p_start date, p_end date) FROM authenticated;
REVOKE ALL ON FUNCTION public.get_users_with_weekly_stats(p_weeks date[]) FROM authenticated;

COMMIT;
