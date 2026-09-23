-- 025: revoke EXECUTE from PUBLIC and anon on the five remaining SECURITY
-- DEFINER functions, and scope the six RLS policies that call them to
-- authenticated (#129)
--
-- 017 already revoked PUBLIC and anon on three of the eight SECURITY
-- DEFINER functions in public (move_playlist_track line 2721,
-- get_active_users_in_period line 2784, get_users_with_weekly_stats line
-- 2838). The other five -- handle_new_user, prevent_role_self_update,
-- is_admin, is_developer_or_higher, is_tester_or_higher -- were left with
-- GRANT ALL ... TO anon (017 lines 2793, 2811, 2820, 2829, 2856) and no
-- REVOKE ... FROM PUBLIC, so PUBLIC still has the default EXECUTE on
-- them. The three 017 closed on its own use a single REVOKE ALL ... FROM
-- PUBLIC line each (017 lines 2721, 2784, 2838) and carry no
-- GRANT ... TO anon; these five do carry that grant, so closing them
-- needs two lines each: FROM PUBLIC and FROM anon.
--
-- Six RLS policies on bug_reports and profiles call one of is_admin(),
-- is_developer_or_higher() or is_tester_or_higher() (017 lines
-- 2313-2348). A policy expression runs with the privileges of the role
-- running the query, not the policy's own role -- revoking anon's
-- EXECUTE without scoping these policies would turn a query from anon
-- against profiles/bug_reports from an empty result into "permission
-- denied for function". Scoping the six to authenticated keeps anon from
-- ever evaluating them, so it keeps seeing empty results, same as today.
-- ALTER POLICY ... TO authenticated changes only the policy's roles; the
-- name, the command and the USING/WITH CHECK expressions are untouched.
-- The three other policies on the same two tables -- "Users can update
-- own profile", "Users can view own profile", "Users can view own
-- reports" (017 lines 2355, 2362, 2369) -- are left without TO on
-- purpose: they compare auth.uid() to the row directly and call no
-- helper, so the REVOKE below does not affect them.
--
-- Order still matters, including outside a single run of this file: if
-- these sixteen statements are ever executed loose, one by one, running
-- the REVOKEs before all six ALTER POLICYs would open a window where
-- anon gets "permission denied for function" from a policy it can still
-- trigger, instead of the empty result it gets today on profiles/
-- bug_reports -- exactly what the policy scoping above exists to avoid.
-- So ALTER POLICY always runs before REVOKE.
--
-- Run as this file, BEGIN/COMMIT below also make it one transaction.
-- None of 001-024 wraps itself in a transaction, and none of the ones
-- applied to the live database changes permissions: they create or
-- replace functions, triggers, tables and indexes, and 017 -- the only
-- file with GRANT/REVOKE -- is a baseline never run against live. This
-- file is the first to change privileges on the live database, in
-- sixteen separate statements: with only the fixed order, a failure
-- partway through would leave it safe but incomplete, and a batch of
-- permission statements applied halfway is tedious to diagnose. The
-- transaction makes it all or nothing.
--
-- No GRANT: none of the five signatures changes, so the existing grants
-- to authenticated and service_role from 017 keep covering them, same
-- reasoning as 018, 021 and 022. No CREATE OR REPLACE: no body changes.
-- If a future migration ever does DROP + CREATE FUNCTION on one of these
-- five, PostgreSQL resets EXECUTE to its default (PUBLIC has it again),
-- and the default privileges 017 sets (line 3111, ALTER DEFAULT
-- PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO
-- anon) also hand anon an explicit grant back; these REVOKEs need to be
-- repeated to close both. CREATE OR REPLACE FUNCTION does not reset
-- either.
--
-- The two triggers bound to handle_new_user() (on_auth_user_created) and
-- to prevent_role_self_update() (enforce_role_change_permission) are not
-- affected: PostgreSQL requires EXECUTE on the trigger function from
-- whoever runs CREATE TRIGGER, not from whoever fires the trigger by
-- doing the INSERT/UPDATE.

BEGIN;

ALTER POLICY "Admins can delete reports" ON public.bug_reports TO authenticated;
ALTER POLICY "Admins can update all profiles" ON public.profiles TO authenticated;
ALTER POLICY "Developers and admins can update reports" ON public.bug_reports TO authenticated;
ALTER POLICY "Developers and admins can view all profiles" ON public.profiles TO authenticated;
ALTER POLICY "Developers and admins can view all reports" ON public.bug_reports TO authenticated;
ALTER POLICY "Testers and above can create reports" ON public.bug_reports TO authenticated;

REVOKE ALL ON FUNCTION public.handle_new_user() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.handle_new_user() FROM anon;
REVOKE ALL ON FUNCTION public.prevent_role_self_update() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.prevent_role_self_update() FROM anon;
REVOKE ALL ON FUNCTION public.is_admin() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.is_admin() FROM anon;
REVOKE ALL ON FUNCTION public.is_developer_or_higher() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.is_developer_or_higher() FROM anon;
REVOKE ALL ON FUNCTION public.is_tester_or_higher() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.is_tester_or_higher() FROM anon;

COMMIT;
