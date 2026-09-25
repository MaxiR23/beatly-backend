-- 030: owner-scoped write policies for play_events, recent_activity and
-- bug_reports, for the user-scoped Supabase client (#143)
--
-- Since #143 the likes, library, playlists, activity, bug reports and
-- profile domains query Supabase as authenticated, with the caller's own
-- JWT, instead of as service_role, which makes RLS load-bearing for these
-- writes for the first time. 017 gives play_events and recent_activity
-- only a SELECT policy each ("play_events readable by owner" line 2473,
-- "recent_activity readable by owner" line 2552) -- neither has an
-- INSERT or UPDATE policy, so an authenticated INSERT into either table
-- needs one added. log_play() (activity_service.log_play) only inserts
-- into play_events, so it needs INSERT there. register_recent() upserts
-- into recent_activity with ON CONFLICT DO UPDATE, so it needs both
-- INSERT (a new row) and UPDATE (an existing one); SELECT is already
-- covered. bug_reports' only INSERT policy, "Testers and above can
-- create reports" (017 line 2348), requires public.is_tester_or_higher()
-- in its WITH CHECK. routes/bug_reports.py's documented contract is that
-- any authenticated user can submit a report (B4, plan-143), so this
-- file widens that policy to any authenticated caller inserting their
-- own report, matching the endpoint's contract; it does not add a role
-- gate to the endpoint (a role gate would be a contract change, out of
-- scope, decided by the repo owner) or touch public.is_tester_or_higher()
-- itself, which is left in place even though this is its last caller
-- (dropping it is a separate change).
--
-- TO authenticated on all three new policies, and only USING (no WITH
-- CHECK) on the one UPDATE: the same form as the 13 owner policies on
-- user-data tables already in 017 (lines 2439-2650), all TO authenticated
-- and comparing auth.uid() to a user_id/owner_id column, and the same
-- form as the UPDATE-by-owner policies on library_items, playlists and
-- user_likes, which declare only USING -- Postgres uses a USING-only
-- UPDATE policy's expression as the WITH CHECK too, so a caller can
-- neither read nor write past their own rows. The operand order in each
-- WITH CHECK/USING below (user_id = auth.uid() on play_events and
-- recent_activity, auth.uid() = reporter_id on bug_reports) matches the
-- table's existing sibling policy, so pg_policies renders it identically
-- to its neighbor rather than reversed.
--
-- DROP + CREATE, not ALTER POLICY, for the bug_reports policy: the WITH
-- CHECK expression itself changes (dropping the is_tester_or_higher()
-- call), and "Testers and above can create reports" would no longer
-- describe what the policy allows, so it is recreated under a name that
-- does, "Users can create own reports" -- ALTER POLICY can only change
-- roles or the USING/WITH CHECK expression in place, not rename a policy
-- to match a changed meaning in one statement in a way that reads clearly
-- here.
--
-- No policy is added for public.tracks: the catalog upsert inside
-- add_track/add_tracks (services/playlist_service.py, _upsert_tracks)
-- keeps running on the service-role client (B1, plan-143) -- tracks has
-- no owner column an RLS policy could scope a write to, and giving
-- authenticated a blanket write policy on the shared catalog is out of
-- scope for this issue.
--
-- Wrapped in BEGIN/COMMIT, the same as 025 (the only other migration
-- that changes policies): a failure partway through a batch of policy
-- statements is tedious to diagnose, and the transaction makes it all or
-- nothing.

BEGIN;

CREATE POLICY "play_events insertable by owner" ON public.play_events FOR INSERT TO authenticated WITH CHECK ((user_id = auth.uid()));

CREATE POLICY "recent_activity insertable by owner" ON public.recent_activity FOR INSERT TO authenticated WITH CHECK ((user_id = auth.uid()));
CREATE POLICY "recent_activity updatable by owner" ON public.recent_activity FOR UPDATE TO authenticated USING ((user_id = auth.uid()));

DROP POLICY "Testers and above can create reports" ON public.bug_reports;
CREATE POLICY "Users can create own reports" ON public.bug_reports FOR INSERT TO authenticated WITH CHECK ((auth.uid() = reporter_id));

COMMIT;
