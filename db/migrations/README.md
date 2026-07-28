# db/migrations

Versioned SQL for the Beatly database. Source of truth for recreating the
new prod database. Origin: `pg_get_functiondef` and `pg_get_triggerdef`
exports run on 2026-07-28, verbatim text of what is running in Supabase
today (except 001, which also includes the constraint applied by hand on
2026-07-27).

## Files

- `001_playlist_tracks_unique_and_add_rpc.sql` — `ux_playlist_track` constraint + `add_playlist_track` RPC (issue #52).
- `002_shared_updated_at.sql` — generic `updated_at` helpers (duplicates of each other, see note inside).
- `003_profiles_auth.sql` — `handle_new_user`, role helpers, `prevent_role_self_update`.
- `004_playlists.sql` — live playlists-domain functions (`move_playlist_track`, `get_owned_playlists_with_track`, thumbnails, `updated_at` bumps, library cleanup).
- `005_playlists_positions.sql` — old position mechanism. `playlist_tracks_reorder` is ACTIVE (see file comment and 009); the other two are suspected dead, unconfirmed.
- `006_genre.sql` — genre thumbnails + `track_count` trigger.
- `007_activity_stats.sql` — weekly aggregation, active-user helpers, `play_events` purge.
- `008_recommendations_feed.sql` — featured, listen again, replay, recommended playlists.
- `009_triggers.sql` — all trigger bindings, verified against the live DB (10 triggers incl. `on_auth_user_created` on `auth.users`; Supabase-internal triggers excluded).
- `010_drop_dead_position_helpers.sql` — drops `move_track_position` and `update_positions` (dead code, legacy app retired).
- `011_add_playlist_tracks_bulk.sql` — set-based bulk add RPC: one atomic round trip for N tracks, dedupe + skip-existing + contiguous positions inside (#55).
- `012_add_playlist_track_lock.sql` — `add_playlist_track` takes the parent playlist row lock (deadlock fix, #55 review).
- `013_playlist_write_protocol.sql` — THE write protocol: every `playlist_tracks` writer is an RPC that locks the parent row first. Reverts the trigger-level attempt, adds the lock to `move_playlist_track`, and adds `remove_playlist_track` to replace the service's direct DELETE (#55 review).
- `012_add_playlist_track_lock.sql` — add_playlist_track acquires the playlist row lock before inserting: consistent lock order with the bulk RPC, fixes a deadlock found in review (#55).
- `013_playlist_write_protocol.sql` — playlist_tracks write protocol: every writer is an RPC that locks the parent playlist row first (reverts the trigger-lock attempt, adds the lock to move, new remove_playlist_track RPC) (#55 review).

## INCOMPLETE — pending for the "schema in the repo" batch

This folder covers functions and trigger bindings. Still to export and
version:

- Table schemas (`CREATE TABLE`), indexes and the remaining constraints
  (only `ux_playlist_track` is here; e.g. `ux_playlist_pos` is referenced
  by `move_playlist_track` but its definition is not versioned).
- RLS policies existing in the old database (the new backend does not use
  RLS; decide what gets ported and what does not).
- Cron jobs / scheduled invocations (the `cron.job` table exists, so
  pg_cron is in use — export `select * from cron.job` to see what calls
  `purge_old_data` and the weekly stats pipeline).

## Findings (recorded, not resolved here)

1. `update_updated_at` and `update_updated_at_column` are identical — both
   active (on `upcoming_releases` and `genre_playlists` respectively).
   Unify when building the new database.
2. `get_playlist_thumbnails` (genre) and `get_user_playlist_thumbnails`
   (user) have near-identical names and different tables — renaming is
   optional, not mixing them up is mandatory.
3. RESOLVED 2026-07-28: `playlist_tracks_reorder` is an ACTIVE trigger
   (`trg_playlist_tracks_reorder`), not dead legacy — it maintains position
   contiguity on delete. `move_track_position` and `update_positions` were
   dead code (no callers, legacy app retired) — dropped in 010.
4. `move_playlist_track` returns `SQLERRM` in the `error` field of its
   JSON; the service surfaces it as `upstream_error` and it never reaches
   the client.

Note: comments INSIDE function bodies are verbatim from the database (some
are in Spanish) — they are part of the exported source and are not edited
here. See TODO below.

## TODO

- [ ] Translate Spanish comments inside function bodies to English (#56).
      Requires CREATE OR REPLACE in Supabase + updating these files in the
      same change, so repo and DB never diverge.