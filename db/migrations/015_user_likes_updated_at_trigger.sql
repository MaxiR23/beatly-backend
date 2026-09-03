-- 015: bind update_updated_at() as a BEFORE UPDATE trigger on user_likes
-- (#75)
-- user_likes never had a trigger (pg_trigger has no rows for
-- public.user_likes), and neither like_track() nor unlike_track() write
-- updated_at themselves, so it never moved past its created_at value.
-- Consequence: GET /likes/sync missed every unlike and every re-like (the
-- ON CONFLICT DO UPDATE branch of the upsert).
-- Reuses update_updated_at() (already declared in 002, already bound to
-- upcoming_releases) instead of declaring a third identical function -- see
-- the README's Findings item 1 for the existing duplication this avoids
-- growing. The bump is unconditional, unlike bump_playlist_updated_at's
-- guard on specific columns: the case that must be reported -- the
-- idempotent re-like, where no column actually changes value -- is not
-- distinguishable by column from a no-op, so a guard in that style would
-- hide exactly the case this fix targets. The accepted price is that two
-- no-observable-change UPDATEs (a repeated DELETE /likes/{track_id} on an
-- already-unliked row, and a repeated POST /likes with identical payload)
-- also re-emit the row on the next sweep -- docs/api/likes.md ("Sweep
-- semantics") already documents both as expected and idempotent.
-- The issue's other question -- whether created_at has a DEFAULT -- is
-- already answered by checking Supabase directly: user_likes.created_at
-- and user_likes.updated_at already have DEFAULT now() and are NOT NULL.
-- There is nothing to fix there, so this migration deliberately does not
-- touch the schema: no ALTER TABLE, only the trigger binding below.

CREATE TRIGGER user_likes_updated_at
  BEFORE UPDATE ON user_likes
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
