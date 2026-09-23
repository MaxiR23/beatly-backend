-- 027: add a nullable fractional order key to playlist_tracks (#133)
--
-- Adds public.playlist_tracks.order_key, a nullable text column with
-- COLLATE "C", plus a common (non-unique) index covering (playlist_id,
-- order_key, id). This is a new file, not an edit to 017 or any other
-- applied migration: the column does not exist yet (confirmed reading
-- 017; no file from 010 to 026 adds it).
--
-- No guards (no IF NOT EXISTS, no CREATE INDEX CONCURRENTLY): drift
-- against what this file expects must fail loudly, not be masked, same
-- reasoning as 024-026. This is also the first forward file (010-026)
-- to create an index; 017's dump carries every index that exists today.
--
-- Wrapped in BEGIN/COMMIT, the same pattern as 025 and 026: the column
-- and its index land together or not at all.
--
-- position, ux_playlist_pos, playlist_tracks_reorder() (current body in
-- 022) and trg_playlist_tracks_reorder are untouched. order_key stays
-- NULL for every row until db/backfills/playlist_tracks_order_key.sql is
-- run by hand; nothing in routes/, services/ or models/ reads or writes
-- it yet.
--
-- This is a normal migration: it applies to the live database and also
-- runs when building a new database from 017 onwards, after 026.

BEGIN;

ALTER TABLE public.playlist_tracks
  ADD COLUMN order_key text COLLATE "C";

CREATE INDEX idx_playlist_tracks_playlist_order_key
  ON public.playlist_tracks (playlist_id, order_key, id);

COMMIT;
