-- 039: keep the recents of user playlists in sync with the playlist (#189)
--
-- A recent_activity row of entity_type 'playlist' and metadata.kind 'user'
-- (037) stores a copy of the playlist's title, and points at the playlist
-- by entity_id (text) with no foreign key. Renaming the playlist left the
-- old title in every recent, and deleting it left recents that open a 404
-- (the home feed showed a playlist that no longer exists). This file keeps
-- the recents in step with the playlist, in the same transaction as the
-- UPDATE or DELETE that changes it.
--
-- Why triggers and not application code: PATCH and DELETE
-- /playlists/{id} run with the caller's own JWT (#143), and recent_activity
-- has no DELETE policy for authenticated, its UPDATE policy (030) is
-- limited to user_id = auth.uid(), and the rows of other users are
-- invisible to that JWT. The recents of other users that point at the
-- playlist (a public playlist someone opened) cannot be reached from the
-- service, and the caller's own recent cannot even be deleted. Same
-- problem as 031 had with library_items, and the same answer.
--
-- SECURITY DEFINER: the functions run as their owner, so RLS does not cut
-- their reach down to the caller's rows. recent_activity belongs to
-- postgres and has no FORCE ROW LEVEL SECURITY, so the owner is not
-- subject to its policies (031's reasoning). SET search_path TO 'public',
-- 'pg_temp', as 018, 021 and 031; every table is also schema-qualified.
--
-- The three statements (rename UPDATE, delete DELETE, one-time cleanup)
-- all filter metadata ->> 'kind' = 'user'. 'genre' playlists live in
-- another table and never exist in public.playlists, and a 'liked' row has
-- entity_id = 'liked', which is not even a uuid: without the filter the
-- cleanup would delete every genre and liked recent. The comparison casts
-- the uuid to text (p.id::text = ra.entity_id), never the text to uuid, as
-- 037 does, so a non-uuid entity_id cannot raise. ->> on a metadata that is
-- not an object yields NULL, so the filter also makes jsonb_set safe.
--
-- played_at is not named anywhere and recent_activity has no triggers, so
-- renaming does not move a recent in the list. The rename trigger is
-- AFTER UPDATE OF title with WHEN (OLD.title IS DISTINCT FROM NEW.title):
-- an UPDATE that does not name title (a PATCH of only description or
-- is_public, or the UPDATE playlists SET updated_at of
-- bump_playlist_on_track_change) does not fire it, and neither does one
-- that sets the same title.
--
-- Privileges: REVOKE ALL ... FROM PUBLIC and FROM anon, as 025 and 031.
-- 017's default privileges (lines 3110-3113 and 3120-3123) give EXECUTE to
-- authenticated and service_role on new functions, and they keep it; no
-- GRANT is added. A trigger function does not need EXECUTE for whoever
-- fires the trigger, only for whoever runs CREATE TRIGGER. No policy is
-- created or changed. Warning, as 025, 031 and 038: a future DROP + CREATE
-- FUNCTION resets the ACL and the default privileges hand EXECUTE back to
-- anon, so both REVOKE lines must be repeated.
--
-- Order: functions, REVOKEs, triggers, cleanup. CREATE TRIGGER takes a
-- SHARE ROW EXCLUSIVE lock on playlists until COMMIT, so no playlist can
-- be deleted between the cleanup and the trigger taking effect: what was
-- deleted before is caught by the cleanup, what is deleted after by the
-- trigger. The cleanup removes the recents (kind 'user') that already
-- point at a playlist that no longer exists. The repo owner counted 0 such
-- rows in the live database on 2026-10-09, and a new database runs it
-- before any row exists, so it deletes 0 rows in both. It is idempotent
-- and the file as a whole is not reversible.
--
-- No guards, no CREATE OR REPLACE, no IF NOT EXISTS: if a name exists
-- already that is drift and the file must fail loudly. BEGIN/COMMIT, like
-- every file that has changed privileges since 025. Normal migration: it
-- applies to the live database and also runs when building a new
-- database, after 038. Apply it BEFORE deploying the PR; the integration
-- tests need it.

BEGIN;

CREATE FUNCTION public.sync_recents_on_playlist_rename()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
begin
  update public.recent_activity
     set metadata = jsonb_set(metadata, '{title}', to_jsonb(new.title))
   where entity_type = 'playlist'
     and metadata ->> 'kind' = 'user'
     and entity_id = new.id::text;
  return new;
end;
$function$;

CREATE FUNCTION public.cleanup_recents_on_playlist_delete()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
begin
  delete from public.recent_activity
   where entity_type = 'playlist'
     and metadata ->> 'kind' = 'user'
     and entity_id = old.id::text;
  return old;
end;
$function$;

REVOKE ALL ON FUNCTION public.sync_recents_on_playlist_rename() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sync_recents_on_playlist_rename() FROM anon;
REVOKE ALL ON FUNCTION public.cleanup_recents_on_playlist_delete() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cleanup_recents_on_playlist_delete() FROM anon;

CREATE TRIGGER trg_sync_recents_on_playlist_rename
  AFTER UPDATE OF title ON public.playlists
  FOR EACH ROW
  WHEN (OLD.title IS DISTINCT FROM NEW.title)
  EXECUTE FUNCTION public.sync_recents_on_playlist_rename();

CREATE TRIGGER trg_cleanup_recents_on_playlist_delete
  AFTER DELETE ON public.playlists
  FOR EACH ROW
  EXECUTE FUNCTION public.cleanup_recents_on_playlist_delete();

DELETE FROM public.recent_activity ra
WHERE ra.entity_type = 'playlist'
  AND ra.metadata ->> 'kind' = 'user'
  AND NOT EXISTS (
      SELECT 1 FROM public.playlists p
      WHERE p.id::text = ra.entity_id
  );

COMMIT;
