# db/migrations

Versioned SQL for the Beatly database. Source of truth for recreating the
new prod database. Two kinds of file live here:

- 001-009 are the baseline: `pg_get_functiondef` and `pg_get_triggerdef`
  exports run on 2026-07-28, verbatim text of what was running in Supabase
  at that point (except 001, which also includes the constraint applied by
  hand on 2026-07-27).
- 010 onwards are forward changes, authored here and applied to Supabase.
  They are not exports, so a later file can supersede part of an earlier
  one — the current state of a function is its last file, not its first.

Applied migrations are never edited. A statement in one that a later file
made obsolete is corrected here, not in the file.

## Files

- `001_playlist_tracks_unique_and_add_rpc.sql` — `ux_playlist_track` constraint + `add_playlist_track` RPC (issue #52).
- `002_shared_updated_at.sql` — generic `updated_at` helpers (duplicates of each other, see note inside).
- `003_profiles_auth.sql` — `handle_new_user`, role helpers, `prevent_role_self_update`.
- `004_playlists.sql` — live playlists-domain functions (`move_playlist_track`, `get_owned_playlists_with_track`, thumbnails, `updated_at` bumps, library cleanup).
- `005_playlists_positions.sql` — old position mechanism. `playlist_tracks_reorder` is ACTIVE (see file comment and 009), and this body is the live one again: 013 restored it verbatim after an attempt to lock the parent inside it. The other two, `move_track_position` and `update_positions`, were dead and are dropped in 010 — kept here as history, not as the current schema.
- `006_genre.sql` — genre thumbnails + `track_count` trigger.
- `007_activity_stats.sql` — weekly aggregation, active-user helpers, `play_events` purge.
- `008_recommendations_feed.sql` — featured, listen again, replay, recommended playlists.
- `009_triggers.sql` — all trigger bindings, verified against the live DB (10 triggers incl. `on_auth_user_created` on `auth.users`; Supabase-internal triggers excluded).
- `010_drop_dead_position_helpers.sql` — drops `move_track_position` and `update_positions` (dead code, legacy app retired).
- `011_add_playlist_tracks_bulk.sql` — set-based bulk add RPC: one atomic round trip for N tracks, dedupe + skip-existing + contiguous positions inside (#55). Its header justifies the `ON CONFLICT` as a safety net for "writers that do not take the playlist lock (e.g. the single-add RPC)" — that describes the state before 012. Since 012 every writer takes the parent lock, so the clause is a pure belt-and-braces now, not a live race. The file itself is left verbatim.
- `012_add_playlist_track_lock.sql` — `add_playlist_track` acquires the parent playlist row lock before inserting: consistent lock order with the bulk RPC, fixes a deadlock found in review (#55).
- `013_playlist_write_protocol.sql` — THE write protocol: every `playlist_tracks` writer is an RPC that locks the parent row first. Reverts the trigger-level lock attempt (restoring `playlist_tracks_reorder` to its 005 body), adds the lock to `move_playlist_track`, and adds `remove_playlist_track` to replace the service's direct DELETE (#55 review).
- `014_add_playlist_track_not_found.sql` — add_playlist_track answers playlist_not_found when the playlist vanished mid-request, aligning it with bulk/remove (#63).
- `015_user_likes_updated_at_trigger.sql` — binds the existing `update_updated_at()` function as a BEFORE UPDATE trigger on `user_likes`, so an unlike or a re-like (the ON CONFLICT DO UPDATE path of the upsert) bumps `updated_at` and is picked up by `GET /likes/sync` (#75). Trigger binding only — no schema change.

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
5. `user_likes_updated_at` (015) only prevents the problem going forward:
   it bumps `updated_at` from the moment it is applied. A row whose unlike
   or re-like happened *before* the trigger existed keeps a stale
   `updated_at` and stays invisible to a `GET /likes/sync` sweep until
   something touches it again. No backfill was run. When 015 was written
   `select count(*) from user_likes where deleted_at is not null` returned
   0, which only says no row was in the unliked state at that moment:
   `like_track()` sends `deleted_at: None`, so a re-like clears the mark
   and an unliked-then-re-liked row counts as 0 too. A re-like (the ON
   CONFLICT DO UPDATE path) does not even need a previous unlike — a
   repeated `POST /likes` on an active row is already an UPDATE. So the
   real reason is that no affected row is identifiable: with the bump
   missing, a re-liked row and an untouched one both have `updated_at =
   created_at`, no query separates them, and the timestamp of the lost
   change cannot be reconstructed.

Note: comments INSIDE function bodies are verbatim from the database (some
are in Spanish) — they are part of the exported source and are not edited
here. See TODO below.

## TODO

- [ ] Translate Spanish comments inside function bodies to English (#56).
      Requires CREATE OR REPLACE in Supabase + updating these files in the
      same change, so repo and DB never diverge.