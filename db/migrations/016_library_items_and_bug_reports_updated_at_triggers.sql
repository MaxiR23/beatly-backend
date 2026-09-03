-- 016: bind update_updated_at() as a BEFORE UPDATE trigger on
-- library_items and bug_reports (#85)
-- library_items never had a trigger, and add_library_item() (in
-- services/library_service.py) never writes updated_at itself, so it
-- never moved past its insert value. Consequence: the idempotent
-- branch of POST /library on an item that already exists (the
-- ON CONFLICT DO UPDATE path of the upsert) updates the row's stored
-- metadata but leaves updated_at frozen at the original insert.
-- bug_reports never had a trigger either, and update_bug_report_status()
-- (in services/bug_report_service.py) only writes status, so updated_at
-- never moved past its created_at value there either. Consequence: every
-- PATCH /bug-reports/{report_id} that changes (or reaffirms) a report's
-- status returns, in that same response, an updated_at that never moved
-- from the insert.
-- Reuses update_updated_at() (already declared in 002, already bound to
-- upcoming_releases in 009 and to user_likes in 015) instead of declaring
-- a third and fourth identical function -- see the README's Findings item
-- 1 for the existing duplication this avoids growing.
-- The bump is unconditional, unlike bump_playlist_updated_at's guard on
-- specific columns: the case that must be reported in each table -- the
-- idempotent POST /library re-add, and the PATCH /bug-reports/{id} that
-- reaffirms the status a report already has -- is a real UPDATE where no
-- column actually changes value, so a guard in that style would hide
-- exactly the case this fix targets.
-- Confirmed against Supabase (pg_trigger swept by the repo owner on
-- 2026-09-03 over every table in the public schema with an updated_at
-- column): before this migration, genre_playlists, playlists, profiles,
-- upcoming_releases and user_likes already had the trigger bound;
-- library_items and bug_reports did not. The sweep is closed -- as of
-- that date, no other table in public is missing the trigger. This
-- migration therefore does not touch the schema: no ALTER TABLE, only
-- the two trigger bindings below.

CREATE TRIGGER library_items_updated_at
  BEFORE UPDATE ON library_items
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER bug_reports_updated_at
  BEFORE UPDATE ON bug_reports
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
