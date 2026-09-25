-- 031: cleanup_library_on_playlist_delete() becomes SECURITY DEFINER,
-- pinned search_path, closed to PUBLIC/anon (#143)
--
-- Since #143 DELETE /playlists/{playlist_id} runs as authenticated, with
-- the deleting user's own JWT (routes/playlists.py, core.auth.get_user_db)
-- instead of service_role. cleanup_library_on_playlist_delete() (017
-- lines 356-367) is the body of trg_cleanup_library_on_playlist_delete
-- (017 line 2124, AFTER DELETE ON public.playlists), and it is SECURITY
-- INVOKER (the default -- 017 declares no SECURITY DEFINER on it): its
-- own DELETE FROM public.library_items therefore runs under the caller's
-- privileges, which "library_items deletable by owner" (017) scopes to
-- rows where user_id = auth.uid(). The cleanup is meant to remove every
-- saved copy of a deleted playlist from every user's library, not just
-- the deleting user's own -- that is the trigger's whole purpose, and is
-- how it behaved under the service-role client, which runs unfiltered by
-- RLS. Under the user-scoped client the same DELETE is filtered to the
-- deleting user's own row (if any), which is narrower than the cleanup
-- is meant to be. The repo owner decided (P1(a), plan-143 addendum) to
-- keep the trigger's original, all-users reach rather than narrow it.
--
-- SECURITY DEFINER makes the function run as its owner (postgres),
-- independent of RLS the same way the service-role client used to be, so
-- the cleanup keeps removing every user's library_items row for the
-- deleted playlist, matching the behavior before #143. The body is
-- copied verbatim from 017 lines 356-367 (LF instead of CRLF, no other
-- change -- see the README entry for the byte-for-byte check); the only
-- relation it touches, public.library_items, is already schema-qualified.
--
-- SET search_path TO 'public', 'pg_temp': the same pinning 018 and 021
-- gave every other SECURITY DEFINER function in this schema, pg_temp
-- listed last so it cannot shadow public. Here it is preventive
-- hardening rather than a closed hole, the same distinction 021's header
-- draws for the five functions that were already schema-qualified: this
-- function's only relation reference is already public.library_items.
--
-- REVOKE ALL ... FROM PUBLIC and FROM anon: SECURITY DEFINER means this
-- function now runs with elevated privileges, so, same as the five
-- functions 025 closed, EXECUTE must not be left open to PUBLIC or anon.
-- 017's ACL for this function (lines 2712-2714) is GRANT ALL ... TO anon,
-- authenticated, service_role with no REVOKE ... FROM PUBLIC -- exactly
-- the shape the five functions in 025 had, so it needs both REVOKE lines,
-- same as those five. CREATE OR REPLACE preserves the existing ACL, so
-- authenticated and service_role keep their grant without being
-- reapplied.
--
-- The trigger binding is unaffected: CREATE OR REPLACE FUNCTION keeps
-- the function's name and signature, so
-- trg_cleanup_library_on_playlist_delete keeps pointing at it, and
-- Postgres requires EXECUTE from whoever runs CREATE TRIGGER, not from
-- whoever fires the trigger by doing the DELETE (025's reasoning,
-- repeated here).
--
-- Warning for the future, same as 025's: a later DROP + CREATE FUNCTION
-- on this function, instead of CREATE OR REPLACE, resets its ACL and
-- hands PUBLIC/anon EXECUTE back via 017's default privileges (line
-- 3111) -- these REVOKEs would need to be repeated.
--
-- Whether library_items rows exist today with a non-owner user_id for a
-- kind='playlist' AND source='external' row (i.e. how much this trigger
-- has already cleaned up under the old, wider reach) was not queried
-- live; see the README entry's note. It does not block this file: (a)
-- preserves the pre-#143 behavior regardless of the answer.
--
-- Wrapped in BEGIN/COMMIT, same as 025's and 030's reasoning: a failure
-- partway through (for example, in one of the two REVOKEs) must not
-- leave the function SECURITY DEFINER with EXECUTE still open to anon.
-- A separate file from 030, not the same transaction: 030's policies and
-- this function change are independent of each other (unlike 025, where
-- ALTER POLICY had to precede REVOKE), so keeping them apart keeps their
-- drift checks apart too -- a drift finding on one does not block
-- applying the other.

BEGIN;

CREATE OR REPLACE FUNCTION public.cleanup_library_on_playlist_delete()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
begin
  delete from public.library_items
   where kind = 'playlist'
     and source = 'external'
     and external_id = old.id::text;
  return old;
end;
$function$;

REVOKE ALL ON FUNCTION public.cleanup_library_on_playlist_delete() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cleanup_library_on_playlist_delete() FROM anon;

COMMIT;
