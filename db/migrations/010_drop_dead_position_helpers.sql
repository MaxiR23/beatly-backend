-- 010: drop dead position helpers
-- Applied in Supabase on 2026-07-28.
-- move_track_position and update_positions were the pre-RPC position
-- mechanism. No callers in the new backend and nothing else hits this
-- database (the legacy app is retired). Dropped to keep the DB clean;
-- they are not part of the new prod database.
-- playlist_tracks_reorder (also in 005) stays: it is ACTIVE (see 009).

DROP FUNCTION IF EXISTS public.move_track_position(uuid, integer, integer);
DROP FUNCTION IF EXISTS public.update_positions(uuid, integer, integer, integer);