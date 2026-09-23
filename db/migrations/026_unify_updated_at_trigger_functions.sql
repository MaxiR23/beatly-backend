-- 026: repoint update_genre_playlists_updated_at to update_updated_at()
-- and drop the duplicate update_updated_at_column() (#131)
--
-- 002 versioned two functions with identical bodies except for casing:
-- update_updated_at() calls NOW() (017 lines 1080-1087) and
-- update_updated_at_column() calls now() (017 lines 1096-1103) — the same
-- function under two names. This closes finding 1 of README.md.
--
-- update_updated_at_column() has exactly one consumer:
-- update_genre_playlists_updated_at on public.genre_playlists (009 line
-- 38, 017 line 2147). update_updated_at() already serves bug_reports,
-- library_items, upcoming_releases and user_likes (015, 016, 017).
--
-- Order matters: the trigger depends on the function, so it is repointed
-- first and the function is dropped after. No CASCADE: an unaccounted
-- dependent must fail the DROP loudly, not be silently removed.
--
-- No guards (no CREATE OR REPLACE TRIGGER, no IF EXISTS): drift against
-- what this file expects must fail loudly, not be masked.
--
-- Wrapped in BEGIN/COMMIT: if CREATE TRIGGER fails after DROP TRIGGER,
-- the whole file rolls back instead of leaving genre_playlists without an
-- updated_at bump.
--
-- No GRANT/REVOKE: update_updated_at()'s signature is unchanged, so the
-- grants 017 already applied to it (lines 2901-2903) keep covering it,
-- same reasoning as 018, 021, 022 and 025. update_updated_at_column()'s
-- grants (017 lines 2910-2912) disappear with the function.
--
-- storage.update_updated_at_column is a Supabase-managed function on
-- storage.objects, a different function in a different schema with the
-- same name. This file does not touch it: every object below is
-- qualified with public.
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 024 and 025.

BEGIN;

DROP TRIGGER update_genre_playlists_updated_at ON public.genre_playlists;

CREATE TRIGGER update_genre_playlists_updated_at
  BEFORE UPDATE ON public.genre_playlists
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

DROP FUNCTION public.update_updated_at_column();

COMMIT;
