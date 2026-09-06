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
- `016_library_items_and_bug_reports_updated_at_triggers.sql` — binds the existing function `update_updated_at()` as a trigger BEFORE UPDATE on `library_items` and `bug_reports`, so the idempotent `POST /library` and the `PATCH /bug-reports/{report_id}` move `updated_at` (#85); trigger binding only, no schema change.
- `017_schema_baseline.sql` — full `pg_dump --schema-only` of the `public` schema, taken on 2026-09-04: 19 tables, 38 `CREATE INDEX` plus 2 `CREATE UNIQUE INDEX` and their constraints, 32 RLS policies spread over 16 tables, with `ENABLE ROW LEVEL SECURITY` on all 19 — `error_logs`, `feed_current` and `release_sync_errors` have RLS on and zero policies, which is deny-all for anyone but `service_role`, 28 functions and 12 triggers (#88). Being a dump of the live database, it carries the current state of everything 001-016 left in `public` — the functions and triggers declared in 002, 009, 015 and 016, the `ux_playlist_track` constraint of 001 plus `ux_playlist_pos`, which no file from 001 to 016 ever declared (004 and 013 only `SET CONSTRAINTS` on it) and which 017 versions for the first time, the functions of 003-008 and the RPCs as 011-014 left them, and correctly without the two helpers 010 dropped. Those files stay as the history of how the schema got here; they are not applied again. From 017 on, a new database starts from this baseline instead of walking 001-016 function by function. One exception, and it matters: `on_auth_user_created` (009) sits on `auth.users`, outside the `public` schema, so it is NOT in this dump — a database built from 017 alone creates no `profiles` row on signup and still needs that one trigger from 009. Operational caveats: it is a baseline, not a forward change, so it contains `CREATE SCHEMA public`, which fails against any database where the public schema already exists — including a fresh Supabase project, which already ships one, and any database created from template1, so that statement has to be skipped or adapted at run time; the file is never run against the live database. It also targets a new Supabase project, not a bare Postgres — it references `auth.users`/`auth.uid()`, grants to `anon`/`authenticated`/`service_role`, calls `gen_random_uuid()` with no `CREATE EXTENSION`, and is wrapped in the psql `\restrict`/`\unrestrict` meta-commands, which fail outside psql.
- `018_move_playlist_track_owner_check.sql` — `move_playlist_track` is `SECURITY DEFINER` and so bypasses RLS; 017's `anon` revoke (finding 7) closed that role's hole but left the `GRANT ... TO authenticated` in place, and the function never checked who was calling it. The new body treats `auth.uid() IS NULL` as a trusted caller — this backend only ever calls the RPC with the service-role client, so that is every legitimate call it makes — and only compares against `playlists.owner_id` when there is a real user JWT behind the call, returning `forbidden` on a mismatch. Also adds the `playlist_not_found` branch that `add_playlist_track` (014) and `remove_playlist_track` (013) already had, and adds `SET search_path TO 'public'`, matching `is_admin`, `handle_new_user`, `prevent_role_self_update`, `is_developer_or_higher` and `is_tester_or_higher`. The signature is unchanged, so the `GRANT`/`REVOKE` already applied in 017 keep covering the function without needing to be reapplied (#90).

## INCOMPLETE — pending for the "schema in the repo" batch

Two of the three gaps this section used to list were closed by
`017_schema_baseline.sql` (dump taken 2026-09-04):

- Table schemas (`CREATE TABLE`), indexes and the remaining
  constraints — CLOSED by 017: 19 `CREATE TABLE`, 38 `CREATE INDEX`
  plus 2 `CREATE UNIQUE INDEX`, and the constraints, including
  `ux_playlist_pos`, which this section used to flag as referenced by
  `move_playlist_track` without being versioned anywhere.
- RLS policies existing in the old database — CLOSED by 017: 32
  `CREATE POLICY` and 19 `ENABLE ROW LEVEL SECURITY`, one per table.
  The export gap is closed; whether the new backend keeps RLS is
  still an open design call, but that is a decision to make, not a
  missing export.

Still open:

- The pg_cron schedule. `cron.job` was queried on 2026-09-06 and
  holds exactly one job: `purge-old-data`, schedule `0 4 * * 0`
  (Sundays 04:00 UTC), command `select purge_old_data()`. There is no
  weekly stats pipeline job — this section used to assume one, and
  the query shows it does not exist. The function it calls,
  `purge_old_data()`, is already versioned in 017; what is still
  missing is the schedule itself, the `cron.schedule('purge-old-data',
  '0 4 * * 0', 'select purge_old_data()')` call, so a database built
  from this folder gets the function but never runs it. 017 does not
  close this: it dumps the `public` schema only, and `cron.job` lives
  in the `cron` schema, so the file has zero occurrences of "cron".

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
6. Full `pg_trigger` sweep, run by the repo owner on 2026-09-03 against
   the `public` schema of the project's Supabase database, over every
   table there with an `updated_at` column: already had the trigger
   bound — `genre_playlists`, `playlists`, `profiles`,
   `upcoming_releases`, `user_likes`. Did not — fixed in `016` —
   `library_items`, `bug_reports`. The sweep is closed: as of that date,
   no other table in `public` is missing the trigger. Do not repeat it
   for any table that already existed then; a table added after
   2026-09-03 is not covered by this sweep.
   Same caveat as item 5: `016` only prevents the problem going forward.
   No row with an already-frozen `updated_at` is corrected retroactively,
   no backfill was run, and today there is no way to identify the
   affected rows — neither `library_items` nor `bug_reports` expose a
   filter for "recently modified", and with the bump missing an updated
   row and an untouched one both have `updated_at` equal to the insert
   value.
7. `SECURITY DEFINER` functions reachable by `anon`. Three of them —
   `move_playlist_track`, `get_active_users_in_period` and
   `get_users_with_weekly_stats` — were executable by the `anon`
   role. Since `SECURITY DEFINER` runs as the owner (`postgres`),
   those calls bypassed RLS entirely, and `move_playlist_track` has
   no owner check of its own, so anyone holding the public anon key
   could reorder another user's playlist. `EXECUTE` was revoked from
   `PUBLIC` and from `anon` on the three of them on 2026-09-04,
   before the dump was taken, so 017 already reflects the fix: each
   of the three carries `REVOKE ALL ON FUNCTION ... FROM PUBLIC` and
   grants only to `authenticated` and `service_role`.
   Two caveats, so this is not read as a clean sweep:
   (a) 017 has 8 `SECURITY DEFINER` functions, not 3. The other five
   — `handle_new_user`, `prevent_role_self_update`, `is_admin`,
   `is_developer_or_higher`, `is_tester_or_higher` — still carry
   `GRANT ALL ... TO anon`, and that was left alone on purpose: the
   first two are trigger functions (they return `trigger` and error
   out if called directly), and the three `is_*` helpers only check
   `profiles` for `auth.uid()`, which is null for an anon caller, so
   they return false. Worth re-checking whenever one of those bodies
   changes; not worth revoking today.
   (b) The revoke closed the anon hole in `move_playlist_track`, not
   the missing owner check: an *authenticated* caller can still
   invoke the RPC for a playlist that is not theirs. The backend does
   check — `move_track()` in `services/playlist_service.py` calls
   `_get_editable_playlist(db, user_id, playlist_id)` before the RPC
   — so no shipped client exercises it, but the function itself does
   not verify ownership. No issue exists for that yet; opening one is
   the repo owner's call and is out of scope for #88.
   RESOLVED 2026-09-06 by `018_move_playlist_track_owner_check.sql`
   (#90): the function now returns `forbidden` when `auth.uid()` is
   non-null and differs from `playlists.owner_id`, and now carries
   `SET search_path TO 'public'` too. Of the trio of `anon`-revoked
   functions named earlier in this finding, the other two —
   `get_active_users_in_period` and `get_users_with_weekly_stats` —
   still have no `SET search_path`; 018 only touches
   `move_playlist_track`.

Note: comments INSIDE function bodies are verbatim from the database (some
are in Spanish) — they are part of the exported source and are not edited
here. See TODO below.

## TODO

- [ ] Translate Spanish comments inside function bodies to English (#56).
      Requires CREATE OR REPLACE in Supabase + updating these files in the
      same change, so repo and DB never diverge.