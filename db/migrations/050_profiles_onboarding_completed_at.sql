-- 050: profiles.onboarding_completed_at, written only by the backend (#223)
--
-- Adds the nullable column public.profiles.onboarding_completed_at: null
-- while the user has not finished the onboarding, the instant of the first
-- close after that. Nobody fills it for the existing users (no backfill): a
-- user created before this file stays null.
--
-- Only the backend writes it, with the service-role client, filtering by the
-- id of the token. So authenticated must not be able to UPDATE it. In
-- Postgres a table-level privilege covers every column, and 017 (lines
-- 3009-3011) grants ALL on public.profiles to authenticated, so a
-- REVOKE UPDATE (onboarding_completed_at) would change nothing. The way is
-- to revoke UPDATE at table level, which also drops any column-level one,
-- and grant it back by column. The seven columns granted are exactly the
-- ones 017 creates (lines 1353-1360); no file from 018 to 049 adds another
-- column to profiles, so authenticated keeps UPDATE on every column it had
-- and not on the new one.
--
-- WARNING: any column added to public.profiles in the future is NOT
-- updatable by authenticated until a migration grants it with a
-- column-level UPDATE grant.
--
-- anon is not touched (it keeps the ALL of 017): no UPDATE of anon reaches a
-- row, because the policy "Users can update own profile" requires
-- auth.uid() = id, which is null for anon, and the admins' policy is
-- TO authenticated since 025. SELECT is not touched for anyone.
--
-- ADD COLUMN nullable without a default does not rewrite the table; it
-- takes ACCESS EXCLUSIVE on profiles for an instant.
--
-- No guards, no CREATE OR REPLACE, no IF NOT EXISTS: if the column exists
-- already that is drift and the file must fail loudly. BEGIN/COMMIT, like
-- every file that has changed privileges since 025. Normal migration: it
-- applies to the live database and also runs when building a new
-- database, after 049. Apply it BEFORE deploying the code: the code selects
-- the column.

BEGIN;

ALTER TABLE public.profiles
  ADD COLUMN onboarding_completed_at timestamp with time zone;

REVOKE UPDATE ON public.profiles FROM authenticated;

GRANT UPDATE (id, username, display_name, avatar_url, role, created_at, updated_at)
  ON public.profiles TO authenticated;

COMMIT;
