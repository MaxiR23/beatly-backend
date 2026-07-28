-- 009: trigger bindings (verified against the live DB on 2026-07-28)
-- Exported via pg_get_triggerdef, verbatim. These attach the trigger
-- functions from 002-006 to their tables. Without these, a fresh database
-- fires nothing.
-- Excluded on purpose: triggers owned by Supabase itself (cron.job,
-- realtime.subscription, storage.*) — they ship with the platform and must
-- not be recreated by hand.
-- Note: on_auth_user_created lives on auth.users (not public); creating
-- triggers on auth tables requires elevated privileges (Supabase SQL
-- editor runs as postgres, which works).

-- auth
CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION handle_new_user();

-- profiles
CREATE TRIGGER enforce_role_change_permission BEFORE UPDATE ON profiles FOR EACH ROW EXECUTE FUNCTION prevent_role_self_update();

CREATE TRIGGER on_profile_updated BEFORE UPDATE ON profiles FOR EACH ROW EXECUTE FUNCTION handle_profile_updated_at();

-- playlists
CREATE TRIGGER trg_bump_playlist_updated_at BEFORE UPDATE ON playlists FOR EACH ROW EXECUTE FUNCTION bump_playlist_updated_at();

CREATE TRIGGER trg_cleanup_library_on_playlist_delete AFTER DELETE ON playlists FOR EACH ROW EXECUTE FUNCTION cleanup_library_on_playlist_delete();

-- playlist_tracks
-- ACTIVE position maintenance: BEFORE trigger keeps positions contiguous
-- (closes the gap on DELETE, shifts on positioned INSERT/UPDATE). The
-- add_playlist_track RPC appends at MAX+1, so the INSERT branch is a no-op
-- for it; the DELETE branch is what keeps positions gapless when the
-- service removes a track. See 005 for the function body and notes.
CREATE TRIGGER trg_playlist_tracks_reorder BEFORE INSERT OR DELETE OR UPDATE ON playlist_tracks FOR EACH ROW EXECUTE FUNCTION playlist_tracks_reorder();

CREATE TRIGGER trg_bump_playlist_on_track_change AFTER INSERT OR DELETE OR UPDATE ON playlist_tracks FOR EACH ROW EXECUTE FUNCTION bump_playlist_on_track_change();

-- genre
CREATE TRIGGER trigger_update_genre_playlist_track_count AFTER INSERT OR DELETE ON genre_playlist_tracks FOR EACH ROW EXECUTE FUNCTION update_genre_playlist_track_count();

CREATE TRIGGER update_genre_playlists_updated_at BEFORE UPDATE ON genre_playlists FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- upcoming_releases
CREATE TRIGGER releases_updated_at BEFORE UPDATE ON upcoming_releases FOR EACH ROW EXECUTE FUNCTION update_updated_at();