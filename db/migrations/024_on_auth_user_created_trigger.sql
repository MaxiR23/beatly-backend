-- 024: declare the on_auth_user_created trigger on auth.users (#127)
--
-- 017 is a dump of the public schema only, so it does not carry this
-- trigger, which sits on auth.users. handle_new_user() is in 017 (current
-- body in 021) but nothing binds it, so a database built from 017 onwards
-- never creates the profiles row on signup. 009 declares the trigger, but
-- 009 is history: from 017 on, a new database starts from the baseline
-- instead of walking 001-016.
--
-- Same trigger as 009 line 13 and as the definition read from the live
-- database on 2026-09-22: same name, table, event, FOR EACH ROW and
-- function. The function is schema-qualified here; pg_get_triggerdef
-- rendered it bare. 017 defines it in public (line 588).
--
-- Trigger binding only, no schema change, same as 015 and 016:
-- handle_new_user() is not redefined here. No GRANT or REVOKE: a CREATE
-- TRIGGER needs none, and the function keeps the privileges 017 applied.
--
-- NOT applied to the live database, and never to be applied there: the
-- trigger already exists there. This file only runs when building a new
-- database from 017 onwards. It is a plain CREATE TRIGGER on purpose, with
-- no guard (no DROP TRIGGER IF EXISTS, no CREATE OR REPLACE): run against
-- the live database by mistake, it fails with 42710 (trigger already
-- exists) and changes nothing. That failure is accepted.
--
-- Creating a trigger on an auth table needs elevated privileges; the
-- Supabase SQL editor runs as postgres, which works (same note as 009).

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
