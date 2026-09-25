# db/migrations

Versioned SQL for the Beatly database. Source of truth for recreating the
new prod database. Two kinds of file live here:

- 001-009 are the baseline: `pg_get_functiondef` and `pg_get_triggerdef`
  exports run on 2026-07-28, verbatim text of what was running in Supabase
  at that point (except 001, which also includes the constraint applied by
  hand on 2026-07-27).
- 010 onwards are forward changes, authored here and applied to Supabase,
  with two exceptions that were never applied to the live database and
  only run when building a new one: 017, a `pg_dump` of the `public`
  schema that a new database starts from, and 024, which declares a
  trigger the live database already has from 009. Their entries below say
  how. Apart from 017, they are not exports, so a later file can supersede
  part of an earlier one — the current state of a function is its last
  file, not its first.

Applied migrations are never edited. A statement in one that a later file
made obsolete is corrected here, not in the file.

Generated data backfills that are meant to be regenerated and re-applied
on purpose are not migrations and do not live here: they live in
`db/backfills/`, with their own README. The first one is
`playlist_tracks_order_key.sql`, filled in by `027` below. Since `029`
(#137) that file, its generator and its test no longer exist; see
`db/backfills/README.md`.

## Files

- `001_playlist_tracks_unique_and_add_rpc.sql` — `ux_playlist_track` constraint + `add_playlist_track` RPC (issue #52).
- `002_shared_updated_at.sql` — generic `updated_at` helpers (duplicates of each other, see note inside). Since `026`, only `update_updated_at()` is live: `update_updated_at_column()` was dropped and the "candidates for unification" note inside is resolved.
- `003_profiles_auth.sql` — `handle_new_user`, role helpers, `prevent_role_self_update`.
- `004_playlists.sql` — live playlists-domain functions (`move_playlist_track`, `get_owned_playlists_with_track`, thumbnails, `updated_at` bumps, library cleanup).
- `005_playlists_positions.sql` — old position mechanism. `playlist_tracks_reorder` is ACTIVE (see file comment and 009), and this body is the live one again: 013 restored it verbatim after an attempt to lock the parent inside it. The other two, `move_track_position` and `update_positions`, were dead and are dropped in 010 — kept here as history, not as the current schema. CORRECTED 2026-09-23 (#135): `trg_playlist_tracks_reorder` is disabled live (`tgenabled = D`, `017` line 2133, measured again on 2026-09-23, ADR 007) — it is not ACTIVE, and nothing keeps `position` contiguous on delete; see finding 3. Since `029` (#137) `playlist_tracks_reorder` no longer exists.
- `006_genre.sql` — genre thumbnails + `track_count` trigger.
- `007_activity_stats.sql` — weekly aggregation, active-user helpers, `play_events` purge.
- `008_recommendations_feed.sql` — featured, listen again, replay, recommended playlists.
- `009_triggers.sql` — all trigger bindings, verified against the live DB (10 triggers incl. `on_auth_user_created` on `auth.users`; Supabase-internal triggers excluded). `026` repoints `update_genre_playlists_updated_at` (line 38) to `update_updated_at()`; the binding in this file is history. Since `029` (#137) the `trg_playlist_tracks_reorder` binding (line 31) is history too.
- `010_drop_dead_position_helpers.sql` — drops `move_track_position` and `update_positions` (dead code, legacy app retired).
- `011_add_playlist_tracks_bulk.sql` — set-based bulk add RPC: one atomic round trip for N tracks, dedupe + skip-existing + contiguous positions inside (#55). Its header justifies the `ON CONFLICT` as a safety net for "writers that do not take the playlist lock (e.g. the single-add RPC)" — that describes the state before 012. Since 012 every writer takes the parent lock, so the clause is a pure belt-and-braces now, not a live race. The file itself is left verbatim.
- `012_add_playlist_track_lock.sql` — `add_playlist_track` acquires the parent playlist row lock before inserting: consistent lock order with the bulk RPC, fixes a deadlock found in review (#55).
- `013_playlist_write_protocol.sql` — THE write protocol: every `playlist_tracks` writer is an RPC that locks the parent row first. Reverts the trigger-level lock attempt (restoring `playlist_tracks_reorder` to its 005 body), adds the lock to `move_playlist_track`, and adds `remove_playlist_track` to replace the service's direct DELETE (#55 review).
- `014_add_playlist_track_not_found.sql` — add_playlist_track answers playlist_not_found when the playlist vanished mid-request, aligning it with bulk/remove (#63).
- `015_user_likes_updated_at_trigger.sql` — binds the existing `update_updated_at()` function as a BEFORE UPDATE trigger on `user_likes`, so an unlike or a re-like (the ON CONFLICT DO UPDATE path of the upsert) bumps `updated_at` and is picked up by `GET /likes/sync` (#75). Trigger binding only — no schema change.
- `016_library_items_and_bug_reports_updated_at_triggers.sql` — binds the existing function `update_updated_at()` as a trigger BEFORE UPDATE on `library_items` and `bug_reports`, so the idempotent `POST /library` and the `PATCH /bug-reports/{report_id}` move `updated_at` (#85); trigger binding only, no schema change.
- `017_schema_baseline.sql` — full `pg_dump --schema-only` of the `public` schema, taken on 2026-09-04: 19 tables, 38 `CREATE INDEX` plus 2 `CREATE UNIQUE INDEX` and their constraints, 32 RLS policies spread over 16 tables, with `ENABLE ROW LEVEL SECURITY` on all 19 — `error_logs`, `feed_current` and `release_sync_errors` have RLS on and zero policies, which is deny-all for anyone but `service_role`, 28 functions and 12 triggers (#88). Being a dump of the live database, it carries the current state of everything 001-016 left in `public` — the functions and triggers declared in 002, 009, 015 and 016, the `ux_playlist_track` constraint of 001 plus `ux_playlist_pos`, which no file from 001 to 016 ever declared (004 and 013 only `SET CONSTRAINTS` on it) and which 017 versions for the first time, the functions of 003-008 and the RPCs as 011-014 left them, and correctly without the two helpers 010 dropped. Those files stay as the history of how the schema got here; they are not applied again. From 017 on, a new database starts from this baseline instead of walking 001-016 function by function. One exception, and it matters: `on_auth_user_created` (009) sits on `auth.users`, outside the `public` schema, so it is NOT in this dump — a database built from 017 alone creates no `profiles` row on signup. `024` declares that trigger, so a database built from 017 onwards gets it without reaching back to 009. Operational caveats: it is a baseline, not a forward change, so it contains `CREATE SCHEMA public`, which fails against any database where the public schema already exists — including a fresh Supabase project, which already ships one, and any database created from template1, so that statement has to be skipped or adapted at run time; the file is never run against the live database. It also targets a new Supabase project, not a bare Postgres — it references `auth.users`/`auth.uid()`, grants to `anon`/`authenticated`/`service_role`, calls `gen_random_uuid()` with no `CREATE EXTENSION`, and is wrapped in the psql `\restrict`/`\unrestrict` meta-commands, which fail outside psql.
- `018_move_playlist_track_owner_check.sql` — `move_playlist_track` is `SECURITY DEFINER` and so bypasses RLS; 017's `anon` revoke (finding 7) closed that role's hole but left the `GRANT ... TO authenticated` in place, and the function never checked who was calling it. The new body treats `auth.uid() IS NULL` as a trusted caller — this backend only ever calls the RPC with the service-role client, so that is every legitimate call it makes — and only compares against `playlists.owner_id` when there is a real user JWT behind the call, returning `forbidden` on a mismatch. Also adds the `playlist_not_found` branch that `add_playlist_track` (014) and `remove_playlist_track` (013) already had, and adds `SET search_path TO 'public'`, matching `is_admin`, `handle_new_user`, `prevent_role_self_update`, `is_developer_or_higher` and `is_tester_or_higher`. The signature is unchanged, so the `GRANT`/`REVOKE` already applied in 017 keep covering the function without needing to be reapplied (#90).
- `019_get_playlist_duration_total.sql` — `get_playlist_duration_total(p_playlist_id uuid)`, a read-only RPC for `GET /playlists/{playlist_id}`'s `total_duration_seconds`. Sums `tracks.duration_seconds` over the `playlist_tracks JOIN tracks` join, with no `.limit()` — it covers every track in the playlist, not just the up-to-1000 rows `_list_playlist_tracks()` reads. Returns `bigint`, the natural type of `SUM()` over an `integer` column, rather than casting back to `integer` and risking an overflow failure for no benefit. Wraps the sum in `COALESCE(..., 0)` so a playlist with no tracks answers `0`, not `NULL` — `SUM()` over zero rows is `NULL` otherwise. `SECURITY INVOKER` (the default, not declared): the backend calls it with the service-role client, which already bypasses RLS, so there is nothing to elevate, and staying invoker keeps a direct `authenticated` caller bound by the `playlist_tracks` visibility policy. Carries `SET search_path TO 'public'`, the bare form used by the five `017` `SECURITY DEFINER` helpers, not `018`'s `'public', 'pg_temp'` — this function creates no temp table and both relations it touches are schema-qualified (#94). Since `#143`, the authenticated `GET /playlists/{playlist_id}` route calls this RPC with the user-scoped client, not `service_role`; the function stays `SECURITY INVOKER` and returns the same result either way, because `"playlist_tracks readable by playlist visibility"` and the other policies it depends on cover the same rows the RPC's own parameter filter already scoped — only the "service-role" framing in the paragraph above is now historical. `019` itself is not edited.
- `020_get_liked_tracks_duration_total.sql` — `get_liked_tracks_duration_total(p_user_id uuid)`, a read-only RPC for `GET /playlists/liked`'s `total_duration_seconds`. Not a generalization of `019`: that function's join and filter are fixed to `playlist_tracks`/`playlist_id`, an applied migration is never edited, and this domain's shape is different, so it is a new function rather than a shared one. Sums `tracks.duration_seconds` over `user_likes JOIN tracks ON tracks.track_id = user_likes.track_id` — the real foreign key, `user_likes_track_id_fkey`, which is on the provider id, unlike `playlist_tracks.track_id`, which is the catalog uuid — filtering `user_likes.user_id = p_user_id AND user_likes.deleted_at IS NULL` so a soft-deleted (unliked) row does not count. Same `RETURNS bigint` and `COALESCE(..., 0)` reasoning as `019`, same explicit-parameter-over-`auth.uid()` reasoning as `019`/`get_owned_playlists_with_track` (the service-role client makes `auth.uid()` null), same `SECURITY INVOKER` default (the "user_likes readable by owner" policy already scopes a direct `authenticated` caller to their own likes), and the same bare `SET search_path TO 'public'` as `019` (#112). Since `#143`, the authenticated `GET /playlists/liked` route calls this RPC with the user-scoped client, not `service_role`; same note as `019`'s: still `SECURITY INVOKER`, same result, because the `SELECT` policies it depends on cover the same rows its own `p_user_id` parameter already scoped. `020` itself is not edited.
- `021_pin_search_path_security_definer.sql` — adds or replaces `SET search_path TO 'public', 'pg_temp'` on the seven `SECURITY DEFINER` functions of `017` that did not already have it in that exact form: `get_active_users_in_period`, `get_users_with_weekly_stats`, `handle_new_user`, `is_admin`, `is_developer_or_higher`, `is_tester_or_higher`, `prevent_role_self_update`. Bodies, signatures, `RETURNS`, `LANGUAGE`, volatility and `SECURITY DEFINER` are unchanged from `017`; no `GRANT`/`REVOKE` — none of the seven signatures changes, so the privileges `017` already applied, including the `GRANT ... TO anon` still in place on five of them (finding 7(a), untouched here), keep covering the function. A new file, not an edit, because `017` is already applied. `get_active_users_in_period` and `get_users_with_weekly_stats` had no `SET search_path` at all and reference `play_events`/`user_weekly_stats` unqualified, so for those two `pg_temp` last is a real close, the same class of hole `018` closed for `move_playlist_track`; the other five already had `SET search_path TO 'public'` with every relation and function they touch schema-qualified, so for those five it is preventive hardening. `move_playlist_track` does not appear here: `018` already gave it `'public', 'pg_temp'` (#120). Since `031` (#143), the "After applying" query below returns 9 rows, not 8 — see that paragraph. `025`'s own privileges query, which filters the same `pg_proc` rows on `prosecdef`, changes the same way for the same reason; that is noted separately in `025`'s entry.

  Before applying, check for drift: for each of the seven, `select pg_get_functiondef('public.<function>(<args>)'::regprocedure);` against its `017` definition should differ only in header layout (`pg_get_functiondef` renders one clause per line and uses the `$function$` tag) and in `\r` — `017`'s dumped bodies are CRLF, this file's are LF, with no semantic effect since no literal in any of the seven spans more than one line. A mechanical version of the same check:
  `select proname, md5(replace(prosrc, E'\r', '')) from pg_proc where pronamespace = 'public'::regnamespace and proname in ('get_active_users_in_period', 'get_users_with_weekly_stats', 'handle_new_user', 'is_admin', 'is_developer_or_higher', 'is_tester_or_higher', 'prevent_role_self_update') order by proname;`
  should return, in that order: `1b435a88ec06e3ef853e1c754cceb263`, `3c42376281f55fd794f5a0212ffa12a1`, `0116b80787691d27df210124014495f2`, `be12ec6bdb2a29375671edaacfe6623c`, `4d51f3d67931449acd50c3c4c198084b`, `4170498489896f517bd8fa74d3e117d9`, `483f4ab9616aa69dfaf34e6d7c4cf3a1`. Each value is the md5 of that function's `017` body — the exact `prosrc` Postgres stores for it, `\r` stripped — computed with this shell recipe, which mirrors `replace(prosrc, E'\r', '')` on the SQL side. Run from the repo root (macOS ships `md5`; Linux ships `md5sum`, which prints the hash followed by `  -`, so take the first field):

  ```sh
  extract() { awk -v fn="FUNCTION public.$2(" 'index($0, fn) && /^CREATE/ {hit=1; next} hit && /AS \$\$/ {body=1; next} body && /^\$\$;/ {exit} body {print}' "$1"; }
  for f in get_active_users_in_period get_users_with_weekly_stats handle_new_user is_admin is_developer_or_higher is_tester_or_higher prevent_role_self_update; do
    printf '%s  ' "$f"
    { printf '\n'; extract db/migrations/017_schema_baseline.sql "$f" | tr -d '\r'; } | md5 -q   # Linux: | md5sum | cut -d' ' -f1
  done
  ```

  `extract` prints every line strictly between the `AS $$` line and the closing `$$;` line for the named function — `017`'s `prosrc` with the wrapping `$$` markers removed, still carrying the `\r` line endings `017`'s dump has. `tr -d '\r'` is the shell-side match for `replace(prosrc, E'\r', '')`. The `printf '\n'` before it puts back the newline that sits between `AS $$` and the first body line in the live `prosrc` — `extract` consumes that line while matching `AS $$` and never prints it, so the newline has to be added back once, not twice: none of the seven bodies opens with a blank line of its own. Recompute with this recipe rather than trust the values above blindly if `017`'s text ever needs re-reading; they were produced by it. If the live database differs in anything beyond `search_path` and line endings, that is drift to treat as a new finding, not something to apply `021` over.

  After applying, verify with
  `select proname, proconfig from pg_proc where pronamespace = 'public'::regnamespace and prosecdef order by proname;`
  which should return exactly eight rows — the seven above plus `move_playlist_track` — every one with `proconfig = {"search_path=public, pg_temp"}` (`pg_temp` last, none `NULL` or any other value). Since `031` (#143) this returns nine rows, not eight: the eight above plus `cleanup_library_on_playlist_delete`, which `031` makes `SECURITY DEFINER` with the same pinned `search_path` this file gives the other seven.
- `022_translate_function_body_comments.sql` — translates the Spanish comments inside the bodies of four functions to English: `add_playlist_track`, `aggregate_user_weekly_stats` and `playlist_tracks_reorder` (current body in `017`), and `move_playlist_track` (current body in `018`; `019`-`021` do not redefine it). 18 comments (19 lines) change from Spanish to English, one comment at a time; everything else — signature, `RETURNS`, `LANGUAGE`, volatility, `SECURITY DEFINER`, `SET search_path` (present only on `move_playlist_track`, unchanged since `018`, `'public', 'pg_temp'`) and logic — stays byte-for-byte the same as the source. A new file, not an edit to `017` or `018`, because both are already applied. No `GRANT`/`REVOKE`: none of the four signatures changes, so the privileges `017` (and, for `move_playlist_track`, `018`) already applied keep covering the function. `aggregate_user_weekly_stats` is the only one of the four whose `017` body is CRLF; `prosrc` moves from `\r\n` to `\n` for it once this is applied, with no semantic effect since no literal in that function spans more than one line, same reasoning as `021` (#56).

  Before applying, check for drift, same pattern as `021`:
  `select proname, md5(replace(prosrc, E'\r', '')) from pg_proc where pronamespace = 'public'::regnamespace and proname in ('add_playlist_track', 'aggregate_user_weekly_stats', 'move_playlist_track', 'playlist_tracks_reorder') order by proname;`
  should return, in that order: `90e78224c697e82d2e1d4160bb856a41`, `7bf973e66731c3872210ef67bc97af6a`, `5575b0ee42545dde87538ad366277789`, `9d0d491876798bf6d90d12093a96d533`. Computed with this recipe, run from the repo root:

  ```sh
  extract() { awk -v fn="FUNCTION public.$2(" 'index($0, fn) && /^CREATE/ {hit=1; next} hit && /AS \$(function)?\$/ {body=1; next} body && /^\$(function)?\$;/ {exit} body {print}' "$1"; }
  printf 'add_playlist_track  '; { printf '\n'; extract db/migrations/017_schema_baseline.sql add_playlist_track | tr -d '\r'; } | md5 -q   # Linux: | md5sum | cut -d' ' -f1
  printf 'aggregate_user_weekly_stats  '; { printf '\n'; extract db/migrations/017_schema_baseline.sql aggregate_user_weekly_stats | tr -d '\r'; } | md5 -q
  printf 'move_playlist_track  '; { printf '\n'; extract db/migrations/018_move_playlist_track_owner_check.sql move_playlist_track | tr -d '\r'; } | md5 -q
  printf 'playlist_tracks_reorder  '; { printf '\n'; extract db/migrations/017_schema_baseline.sql playlist_tracks_reorder | tr -d '\r'; } | md5 -q
  ```

  The only difference from `021`'s recipe: `extract`'s regex accepts both `$$` and `$function$` as the body delimiter, because `move_playlist_track`'s source (`018`) already uses `$function$`, not `017`'s `$$`. The `printf '\n'`, the `tr -d '\r'`, and everything else about what the recipe matches on the SQL side are the same as `021`'s explanation above; not repeated here. If the live database differs in anything beyond line endings, that is drift to treat as a new finding, not something to apply `022` over.

  After applying, verify with the same query pointed at the same four functions; it should return `2e3ddcc187a6ff31763ac29800ca6472`, `276126802a74e6b2f6d83ece0296eb4f`, `9d8cee730a34ea41e5600525a62e6bef`, `43cbfc771352861b14e34d141636f2d3`, in that order — different from the pre-apply values above, because the comments changed — computed with the same recipe pointed at `022` instead of `017`/`018`.

  `select proname, prosecdef, provolatile, proconfig from pg_proc where pronamespace = 'public'::regnamespace and proname in ('add_playlist_track', 'aggregate_user_weekly_stats', 'move_playlist_track', 'playlist_tracks_reorder') order by proname;`
  before and after applying `022` should both return `move_playlist_track` with `prosecdef = true` and `proconfig = {"search_path=public, pg_temp"}`, the other three with `prosecdef = false` and `proconfig` `NULL`, all four with `provolatile = 'v'` — same criterion as the md5 check above: if it does not return that before applying, that is drift, and `022` does not apply over it.

  `select tgenabled from pg_trigger where tgname = 'trg_playlist_tracks_reorder';` before and after applying `022` should return the same value — `CREATE OR REPLACE FUNCTION` does not change a trigger's enabled state. `017`'s dump has this trigger `DISABLE`d (line 2133); this entry does not assert whether that is still the live state.
- `023_align_genre_playlist_thumbnail_filters.sql` — adds
  `AND t.thumbnail_url <> ''` to the `WHERE` of **two** functions that
  build thumbnail mosaics over `genre_playlist_tracks JOIN tracks`:
  `get_playlist_thumbnails` (current body in `017`; `018`-`022` do not
  touch it) and `recommend_playlists_by_history` (current body in `017`,
  identical to `008`'s once `\r` is normalized; `018`-`022` do not touch
  it either). This leaves all three functions in the repo that filter
  `tracks.thumbnail_url` with the same two conditions; the third,
  `get_user_playlist_thumbnails`, already had both since `004` and is not
  touched. Bodies copied verbatim except for that one line and the `\r`,
  normalized to LF same as `021`/`022`, with no semantic effect. A new
  file, not an edit to `006`/`008`/`017`, because all three are already
  applied. No `GRANT`/`REVOKE`: neither signature changes, so the
  privileges `017` already applied (lines 2754-2759 for one, 2874-2876
  for the other) keep covering them. `public.tracks.thumbnail_url` is
  `NOT NULL` (`017` line 1429), so the existing `IS NOT NULL` never
  discarded anything in either function and it is the `<> ''` that closes
  the gap. `recommend_playlists_by_history` has no caller in the backend
  yet: this is a preventive fix. Volatility: `get_playlist_thumbnails`
  stays `STABLE`, `recommend_playlists_by_history` stays `VOLATILE`;
  neither is `SECURITY DEFINER` (#125).

  One migration, two objects: if the drift check below passes for one
  function and fails for the other, `023` does not apply — the file is
  atomic in practice because the owner runs it whole. Applying `023`
  with drift on only one of the two would silently overwrite a hand-made
  change in Supabase on the other.

  Before applying, check for drift, same pattern as `021`/`022`, one
  query for both:

  ```
  select proname, md5(replace(prosrc, E'\r', '')) from pg_proc where pronamespace = 'public'::regnamespace and proname in ('get_playlist_thumbnails', 'recommend_playlists_by_history') order by proname;
  ```

  should return, in that order, `f90575432fc0b2d9e6f7445503fd483b` and
  `17db2963afb70fcf4fb7b446ce937c9e`, the md5 of the `017` bodies without
  `\r`, computed with the same `extract` recipe already written above in
  the `022` entry — referenced the way `022` references `021`'s, not
  repeated in full:

  ```sh
  extract() { awk -v fn="FUNCTION public.$2(" 'index($0, fn) && /^CREATE/ {hit=1; next} hit && /AS \$(function)?\$/ {body=1; next} body && /^\$(function)?\$;/ {exit} body {print}' "$1"; }
  printf 'get_playlist_thumbnails  ';        { printf '\n'; extract db/migrations/017_schema_baseline.sql get_playlist_thumbnails | tr -d '\r'; } | md5 -q   # Linux: | md5sum | cut -d' ' -f1
  printf 'recommend_playlists_by_history  '; { printf '\n'; extract db/migrations/017_schema_baseline.sql recommend_playlists_by_history | tr -d '\r'; } | md5 -q
  ```

  The recipe needs no new variant: `get_playlist_thumbnails` and
  `recommend_playlists_by_history` both use the `$$` delimiter in `017`,
  which `extract` already accepts since `022`.

  And the structural invariant, same idea as `022`'s second query:

  ```
  select proname, prosecdef, provolatile, proconfig from pg_proc where pronamespace = 'public'::regnamespace and proname in ('get_playlist_thumbnails', 'recommend_playlists_by_history') order by proname;
  ```

  before and after should both return the two rows with `prosecdef =
  false` and `proconfig` `NULL`, `get_playlist_thumbnails` with
  `provolatile = 's'` and `recommend_playlists_by_history` with
  `provolatile = 'v'`. If the hash or these values do not match before
  applying, that is drift to report as a new finding, not something to
  apply `023` over.

  After applying, verify with the same md5 query pointed at the same two
  functions: it should now return `6b7744c307095b2c8ba88a945a82ac0d` and
  `dbeded673cccd3c4d3ffa101d6d44127`, in that order (the `replace(prosrc,
  E'\r', '')` is a no-op, because the new bodies are LF), computed with
  the same recipe pointed at `023` instead of `017`. Plus the parity
  check, which reads without computing anything and now covers all three
  functions:

  ```
  select proname,
         prosrc like '%thumbnail_url IS NOT NULL%' as has_null_filter,
         prosrc like '%thumbnail_url <> ''''%'   as has_empty_filter
  from pg_proc
  where pronamespace = 'public'::regnamespace
    and proname in ('get_playlist_thumbnails', 'get_user_playlist_thumbnails', 'recommend_playlists_by_history')
  order by proname;
  ```

  should return three rows, all three with `has_null_filter` and
  `has_empty_filter` `true`.
- `024_on_auth_user_created_trigger.sql` — declares `on_auth_user_created` `AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.handle_new_user()`, the one binding `017` cannot carry because it dumps only the `public` schema, so a database built from `017` onwards creates the `profiles` row on signup without reaching back to `009` (#127). Trigger binding only — no schema change: `handle_new_user()` is already versioned (`017`, current body in `021`) and is not redefined here; no `GRANT`/`REVOKE`, since a `CREATE TRIGGER` needs none and the function keeps the privileges `017` applied. Same trigger as `009` line 13 and as the definition read from the live database on 2026-09-22 — name, table, event, `FOR EACH ROW` and function; the only textual difference is that the function is schema-qualified here, where `pg_get_triggerdef` rendered it bare, and `017` defines it in `public`. This file was not applied to the live database and is never applied there: the trigger already exists there, it is what `009` exported. It only runs when building a new database from `017` onwards. It is a plain `CREATE TRIGGER`, with no guard and no defensive DDL — no `DROP TRIGGER IF EXISTS`, no `CREATE OR REPLACE` — so if it is ever run against the live database by mistake it fails with `42710` (trigger already exists) and changes nothing; that failure is accepted, on purpose.
- `025_revoke_anon_execute_scope_helper_policies.sql` — revokes `EXECUTE`
  from `PUBLIC` and from `anon` on the five `SECURITY DEFINER` functions
  `017` left open: `handle_new_user`, `prevent_role_self_update`,
  `is_admin`, `is_developer_or_higher`, `is_tester_or_higher` (`017`
  lines 2793, 2811, 2820, 2829, 2856 each carry `GRANT ALL ... TO anon`
  and no `REVOKE ... FROM PUBLIC`). `017` closed the three it closed on
  its own (`move_playlist_track`, `get_active_users_in_period`,
  `get_users_with_weekly_stats`, lines 2721, 2784, 2838) with a single
  `REVOKE ALL ... FROM PUBLIC` line each and no `GRANT ... TO anon`;
  these five do carry that grant, so closing them needs two lines each:
  `FROM PUBLIC` and `FROM anon`. Also scopes to `authenticated`, with `ALTER POLICY
  ... TO authenticated`, the six RLS policies on `bug_reports` and
  `profiles` that call `is_admin()`, `is_developer_or_higher()` or
  `is_tester_or_higher()` (`017` lines 2313-2348) — a policy expression
  runs with the privileges of the role running the query, not the
  policy's own role, so revoking `anon`'s `EXECUTE` without scoping these
  policies would turn a query from `anon` against `profiles`/
  `bug_reports` from an empty result into `permission denied for
  function`. `ALTER POLICY` changes only the policy's `roles`; name,
  command and `USING`/`WITH CHECK` are untouched, so the predicates stay
  identical to `017`. The other three policies on the same two tables
  (`Users can update own profile`, `Users can view own profile`, `Users
  can view own reports`, `017` lines 2355, 2362, 2369) are left without
  `TO` on purpose — they compare `auth.uid()` to the row directly and
  call no helper, so the `REVOKE` does not affect them; uniforming their
  role declaration is a separate, out-of-scope change. The file's order
  is required and has no exception: the six `ALTER POLICY` statements run
  before the ten `REVOKE`, even outside a single run of this file — if
  the sixteen statements are ever executed loose, one by one, reversing
  that order would give `anon` a window of `permission denied` instead of
  an empty result on `profiles`/`bug_reports`, the exact outcome the
  policy scoping exists to avoid. Run as this file, the whole thing is
  also wrapped in `BEGIN`/`COMMIT`. None of `001`-`024` wraps itself in
  a transaction, and none of the ones applied to the live database
  changes permissions: they create or replace functions, triggers,
  tables and indexes, and `017` — the only file with `GRANT`/`REVOKE` —
  is a baseline never run against live. This file is the first to
  change privileges on the live database, in sixteen separate
  statements: with only the fixed order, a failure partway through
  would leave it safe but incomplete, and a batch of permission
  statements applied halfway is tedious to diagnose. The transaction
  makes it all or nothing. No `GRANT`: none of the five
  signatures changes, so the
  grants to `authenticated` and `service_role` `017` already applied keep
  covering them, same reasoning as `018`, `021` and `022`. No `CREATE OR
  REPLACE`: no function body changes. The two triggers bound to
  `handle_new_user()` and `prevent_role_self_update()`
  (`on_auth_user_created`, `enforce_role_change_permission`) are not
  affected: `CREATE TRIGGER` needs `EXECUTE` from whoever declares the
  trigger, not from whoever fires it by doing the `INSERT`/`UPDATE`. If a
  future migration ever does `DROP` + `CREATE FUNCTION` on one of the
  five, `PUBLIC` gets its default `EXECUTE` back and the default
  privileges `017` sets (line 3111, `ALTER DEFAULT PRIVILEGES FOR ROLE
  postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon`) also hand
  `anon` an explicit grant back; these `REVOKE`s need to be repeated to
  close both — `CREATE OR REPLACE FUNCTION` does not reset either.
  This is a normal migration: applied to the live database, and also run
  when building a new database, after `024` (#129). This closes the
  `GRANT ... TO anon` the `021` entry still describes as in place
  (finding 7(a)).

  Before applying, check for drift. Four queries, since there is no body
  to md5 — no `CREATE OR REPLACE` here.

  Privileges, derived from `017`'s `GRANT`/`REVOKE` rather than compared
  as text, with `has_function_privilege` per role and function:

  ```
  select p.proname,
         has_function_privilege('anon', p.oid, 'EXECUTE') as anon,
         has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated,
         has_function_privilege('service_role', p.oid, 'EXECUTE') as service_role
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace and p.prosecdef
  order by p.proname;
  ```

  should return exactly 8 rows:

  | proname | anon | authenticated | service_role |
  |---|---|---|---|
  | get_active_users_in_period | f | t | t |
  | get_users_with_weekly_stats | f | t | t |
  | handle_new_user | t | t | t |
  | is_admin | t | t | t |
  | is_developer_or_higher | t | t | t |
  | is_tester_or_higher | t | t | t |
  | move_playlist_track | f | t | t |
  | prevent_role_self_update | t | t | t |

  `018`-`024` contain no `GRANT`/`REVOKE` (`018`, `021` and `022` use
  `CREATE OR REPLACE`, which preserves the ACL), so `017` is still the
  source of the privileges expected here. Filtering by `prosecdef` makes
  a ninth `SECURITY DEFINER` function or an overload visible as an extra
  row, and matching by `oid` avoids the ambiguity a name match would have
  with an overload. If the query does not return this table, that is
  drift to report as a new finding, and `025` does not apply over it.

  Policies, comparing `pg_policies` against `017` the same way `021`-`023`
  compare function bodies:

  ```
  set search_path to '';
  select tablename, policyname, permissive, roles, cmd, qual, with_check
  from pg_catalog.pg_policies
  where schemaname = 'public' and tablename in ('bug_reports', 'profiles')
  order by tablename, policyname;
  ```

  should return exactly 9 rows, all `permissive = PERMISSIVE` and `roles
  = {public}`:

  | tablename | policyname | cmd | qual | with_check |
  |---|---|---|---|---|
  | bug_reports | Admins can delete reports | DELETE | public.is_admin() | NULL |
  | bug_reports | Developers and admins can update reports | UPDATE | public.is_developer_or_higher() | NULL |
  | bug_reports | Developers and admins can view all reports | SELECT | public.is_developer_or_higher() | NULL |
  | bug_reports | Testers and above can create reports | INSERT | NULL | ((auth.uid() = reporter_id) AND public.is_tester_or_higher()) |
  | bug_reports | Users can view own reports | SELECT | (auth.uid() = reporter_id) | NULL |
  | profiles | Admins can update all profiles | UPDATE | public.is_admin() | NULL |
  | profiles | Developers and admins can view all profiles | SELECT | public.is_developer_or_higher() | NULL |
  | profiles | Users can update own profile | UPDATE | (auth.uid() = id) | (auth.uid() = id) |
  | profiles | Users can view own profile | SELECT | (auth.uid() = id) | NULL |

  This text is comparable with `017`'s `CREATE POLICY` source: `pg_dump`
  writes `USING (<expr>)`/`WITH CHECK (<expr>)` with `pg_get_expr` under
  an empty `search_path` (`017` line 49,
  `set_config('search_path', '', false)`), and `pg_policies` renders
  `qual`/`with_check` with the same `pg_get_expr`; `set search_path to
  ''` in the same session makes them come out qualified the same way. If
  they come out without `public.` (e.g. `is_admin()`), the `set` did not
  take in that session — repeat the two statements together, that is not
  drift on its own. All 9 rows are listed, not only the 6 the `ALTER
  POLICY`s touch, so the after-applying query can show the 3 `Users
  can ...` policies were not touched. If the before-applying values do
  not match, that is drift to report as a new finding, same criterion as
  `021`-`023`, and `025` does not apply over it.

  Callers, because the two queries above only see the six policies this
  file already knows about — they do not rule out some other policy,
  view or function elsewhere in the database also calling one of the
  three `is_*` helpers, which `anon`'s `REVOKE` would turn from an empty
  result into `permission denied for function`. Two more queries, across
  every schema, not only `public`:

  a) Dependencies `pg_catalog.pg_depend` registers for a policy's or a
  view's expression:

  ```
  select d.classid::regclass as catalog,
         pg_catalog.pg_describe_object(d.classid, d.objid, d.objsubid) as dependent,
         d.refobjid::regprocedure as helper
  from pg_catalog.pg_depend d
  where d.refclassid = 'pg_catalog.pg_proc'::regclass
    and d.refobjid in ('public.is_admin()'::regprocedure,
                       'public.is_developer_or_higher()'::regprocedure,
                       'public.is_tester_or_higher()'::regprocedure)
  order by 2;
  ```

  should return exactly 6 rows, all `catalog = pg_policy`, one per
  policy in the table above that calls a helper — the four on
  `bug_reports` and the two on `profiles` (`017` lines 2313-2348). A row
  with another `catalog` (for example `pg_rewrite`, a view) or a policy
  on another table or schema is drift. (`dependent` may render with or
  without the `public.` prefix depending on `search_path`; judge it by
  catalog, policy name and table, not by that text.)

  b) Calls inside function bodies, which `pg_depend` does not register
  for non-atomic `sql`/`plpgsql` bodies:

  ```
  select p.oid::regprocedure as caller, p.prosecdef
  from pg_catalog.pg_proc p
  where p.prosrc ~ '\m(is_admin|is_developer_or_higher|is_tester_or_higher)\s*\('
    and p.oid not in ('public.is_admin()'::regprocedure,
                      'public.is_developer_or_higher()'::regprocedure,
                      'public.is_tester_or_higher()'::regprocedure)
  order by 1;
  ```

  should return exactly 1 row, `public.prevent_role_self_update()` with
  `prosecdef = t` (`017` line 906, current body `021` line 156 — it runs
  as `postgres`, so the `REVOKE`s below do not affect it). An extra row
  that calls `public.is_*` is drift; an extra row that turns out to be
  an unrelated function using its own identifier with that name, not
  `public.is_*`, is worth noting but is not drift.

  If either (a) or (b) does not return exactly what is described above,
  that is drift to report as a new finding, same criterion as
  `021`-`023`, and `025` does not apply over it.

  After applying, verify with the same four queries. Privileges: 8 rows,
  `anon = f` on all 8, `authenticated = t` and `service_role = t` on all
  8 — `anon = f` under `has_function_privilege` also implies `PUBLIC` has
  no `EXECUTE`, because every role inherits `PUBLIC`'s privileges, so
  this single query covers "none of the eight grants `EXECUTE` to
  `PUBLIC` or to `anon`". Policies: the same 9 rows, `qual`/`with_check`/
  `cmd`/`permissive` unchanged, `roles = {authenticated}` on the six that
  call a helper and `roles = {public}` on the three `Users can ...`
  policies. Callers, (a) and (b): the same rows as before applying —
  `ALTER POLICY` changes only a policy's `roles`, not what it depends on
  or any function body, so neither query's result moves.

  Since `030` (#143), the policy `"Testers and above can create
  reports"` no longer exists — `030` drops it and replaces it with
  `"Users can create own reports"`, which does not call
  `is_tester_or_higher()` — so the Policies query above still returns 9
  rows, but one of them changes: the `bug_reports` INSERT row's
  `policyname` reads `Users can create own reports` and its `with_check`
  reads `(auth.uid() = reporter_id)`, dropping the
  `AND public.is_tester_or_higher()` clause; `roles` stays
  `{authenticated}` (set by this file's own `ALTER POLICY`) and `cmd`
  stays `INSERT`. The other 8 rows are unchanged. And query (a) above
  (dependencies on `is_admin`/`is_developer_or_higher`/
  `is_tester_or_higher`) now returns
  5 rows, not 6: the row for that policy is gone, the other 3
  `bug_reports` rows and the 2 `profiles` rows are unchanged. Since `031`
  (#143), the privileges query above (filtered on `prosecdef`) returns 9
  rows, not 8: the ninth is `cleanup_library_on_playlist_delete`, `anon =
  f`, `authenticated = t`, `service_role = t` — `031` makes it `SECURITY
  DEFINER` and revokes `PUBLIC`/`anon` the same way this file did for the
  other five. Since `032` (#146), the privileges query above reads
  `authenticated = f` on the `get_active_users_in_period` and
  `get_users_with_weekly_stats` rows; `anon` and `service_role` are
  unchanged on both.
- `026_unify_updated_at_trigger_functions.sql` — repoints
  `update_genre_playlists_updated_at` to `public.update_updated_at()`
  (same name, table, `BEFORE UPDATE` and `FOR EACH ROW` as `017` line
  2147) and drops `public.update_updated_at_column()`. With this,
  `update_updated_at()` serves all five tables that bump `updated_at`:
  `bug_reports`, `genre_playlists`, `library_items`,
  `upcoming_releases` and `user_likes`. No observable change: `NOW()`
  and `now()` are the same function. A new file, not an edit to `002`,
  `009` or `017`, because all three are already applied. Repoint
  before drop, no `CASCADE`, no guards (`IF EXISTS`, `CREATE OR
  REPLACE TRIGGER`) — same reasoning as the entries above: the trigger
  depends on the function (`DROP FUNCTION` without `CASCADE` would
  fail otherwise), and drift has to fail loudly rather than be masked.
  Wrapped in `BEGIN`/`COMMIT`, the second file to use a transaction
  after `025` — here so a `CREATE TRIGGER` failure right after `DROP
  TRIGGER` cannot leave `genre_playlists` without its `updated_at`
  bump. No `GRANT`/`REVOKE`: `update_updated_at()`'s signature is
  unchanged, so the grants `017` already applied to it (lines
  2901-2903) keep covering it, same reasoning as `018`, `021`, `022`
  and `025`; the grants on `update_updated_at_column()` (`017` lines
  2910-2912) disappear with the `DROP`. `storage.update_updated_at_column`
  is a different, Supabase-managed function of the same name on
  `storage.objects`; it is not touched, and each query below filters
  by schema for that reason — comparing by `proname` alone would make
  it look like a consumer of the function this file drops. This is a
  normal migration: it applies to the live database and also runs
  when building a new database from `017` onwards, after `024` and
  `025` (#131). Closes finding 1.

  Before applying, check for drift. Four queries.

  (1) Current binding, comparable with `017` line 2147:

  ```
  set search_path to '';
  select t.tgname, t.tgenabled, pg_catalog.pg_get_triggerdef(t.oid) as def
  from pg_catalog.pg_trigger t
  join pg_catalog.pg_class c on c.oid = t.tgrelid
  join pg_catalog.pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relname = 'genre_playlists'
    and not t.tgisinternal
  order by t.tgname;
  ```

  should return exactly 1 row, `update_genre_playlists_updated_at`,
  `tgenabled = O`, `def` = `CREATE TRIGGER
  update_genre_playlists_updated_at BEFORE UPDATE ON
  public.genre_playlists FOR EACH ROW EXECUTE FUNCTION
  public.update_updated_at_column()` (`017` line 2147 without the
  trailing `;`). If it comes back without `public.`, the `set` did not
  take in that session — run the two statements together; that alone
  is not drift, same note as `025`. `017` declares no other
  non-internal trigger on `genre_playlists`
  (`trigger_update_genre_playlist_track_count` is on
  `genre_playlist_tracks`, a different table).

  (2) Every trigger, in any schema, that runs either of the two
  `public` functions:

  ```
  select p.proname, n.nspname as table_schema, c.relname, t.tgname
  from pg_catalog.pg_trigger t
  join pg_catalog.pg_proc p on p.oid = t.tgfoid
  join pg_catalog.pg_namespace pn on pn.oid = p.pronamespace
  join pg_catalog.pg_class c on c.oid = t.tgrelid
  join pg_catalog.pg_namespace n on n.oid = c.relnamespace
  where pn.nspname = 'public'
    and p.proname in ('update_updated_at', 'update_updated_at_column')
    and not t.tgisinternal
  order by p.proname, n.nspname, c.relname;
  ```

  should return exactly 5 rows: `update_updated_at` on `public` /
  `bug_reports` / `bug_reports_updated_at`, `public` / `library_items`
  / `library_items_updated_at`, `public` / `upcoming_releases` /
  `releases_updated_at`, `public` / `user_likes` /
  `user_likes_updated_at`, and `update_updated_at_column` on `public`
  / `genre_playlists` / `update_genre_playlists_updated_at` — from
  `017` lines 2075, 2089, 2103, 2147 and 2154. The `pn.nspname =
  'public'` filter excludes the trigger on `storage.objects`.

  (3) Every object that depends on the function being dropped, not
  only triggers:

  ```
  set search_path to '';
  select d.classid::regclass as catalog,
         pg_catalog.pg_describe_object(d.classid, d.objid, d.objsubid) as dependent,
         d.deptype
  from pg_catalog.pg_depend d
  join pg_catalog.pg_proc p on p.oid = d.refobjid
  join pg_catalog.pg_namespace pn on pn.oid = p.pronamespace
  where d.refclassid = 'pg_catalog.pg_proc'::regclass
    and pn.nspname = 'public' and p.proname = 'update_updated_at_column';
  ```

  should return exactly 1 row: `catalog = pg_trigger`, `dependent =
  trigger update_genre_playlists_updated_at on table
  public.genre_playlists`, `deptype = n`. Filters by name and schema,
  not `::regprocedure`, so the same query can run again after
  applying, once the function no longer exists. No need to also
  search inside function bodies (`025` did, for its `prosrc` case): a
  function that `RETURNS trigger` only runs as a trigger, and calling
  it directly errors out.

  (4) Inventory of functions with either name in `public` and
  `storage`, with the body of the `public` ones normalized:

  ```
  select p.oid, n.nspname, p.proname,
         pg_catalog.pg_get_function_identity_arguments(p.oid) as args,
         p.prosecdef, p.proconfig,
         case when n.nspname = 'public'
              then regexp_replace(p.prosrc, '\s+', ' ', 'g') end as body
  from pg_catalog.pg_proc p
  join pg_catalog.pg_namespace n on n.oid = p.pronamespace
  where n.nspname in ('public', 'storage')
    and p.proname in ('update_updated_at', 'update_updated_at_column')
  order by n.nspname, p.proname;
  ```

  should return two `public` rows exactly: `update_updated_at`, empty
  `args`, `prosecdef = f`, `proconfig` `NULL`, `body` = ` BEGIN
  NEW.updated_at = NOW(); RETURN NEW; END; `; and
  `update_updated_at_column`, the same shape with `now()` in
  lowercase. Both `body` values come from applying the same
  normalization (`\s+` to one space) to the `017` bodies at lines
  1080-1087 and 1096-1103, so they hold whether the live `prosrc` has
  `\r\n` (as `017` does) or `\n`. A third row, `storage` /
  `update_updated_at_column`, is expected too — note its `oid`, which
  the after-applying query compares. Extra `public` rows (an
  overload), a different `body`, or `prosecdef = t` are drift. If the
  `storage` row is missing, note it but do not block `026` on it — the
  file does not depend on that function — and skip the `oid`
  comparison after applying.

  If any of the four queries does not return exactly what is
  described above, that is drift to report as a new finding, and
  `026` does not apply over it.

  After applying, verify with the same four queries.
  - (1): 1 row, same `tgname`, `tgenabled = O`, `def` = `CREATE
    TRIGGER update_genre_playlists_updated_at BEFORE UPDATE ON
    public.genre_playlists FOR EACH ROW EXECUTE FUNCTION
    public.update_updated_at()`.
  - (2): the same 5 rows, now all 5 with `proname =
    update_updated_at`.
  - (3): 0 rows. The query avoids `::regprocedure` precisely because
    the function no longer exists and the cast would fail instead of
    returning empty.
  - (4): 2 rows: `public.update_updated_at` with the same `body`, and
    `storage.update_updated_at_column` with the `oid` noted before
    applying — the same `oid` means the same object, not a recreated
    one.
  - Functional check, leaving no trace:

    ```
    begin;
    update public.genre_playlists set sort_order = sort_order
    where id = (select id from public.genre_playlists limit 1)
    returning updated_at = now() as bumped;
    rollback;
    ```

    should return `bumped = t`: `now()` is the start time of the
    transaction, the same value the trigger writes. `id` and
    `sort_order` exist on `genre_playlists` (`017`).

- `027_add_playlist_tracks_order_key.sql` — adds
  `public.playlist_tracks.order_key`, a nullable `text` column with
  `COLLATE "C"`, plus `idx_playlist_tracks_playlist_order_key`, a common
  (non-unique) index covering `(playlist_id, order_key, id)`. A new file,
  not an edit to `017`: the column does not exist there, and no file from
  `010` to `026` adds it. The first forward file (`010`-`026`) to create
  an index — every index on `playlist_tracks` today comes from the `017`
  dump. `BEGIN`/`COMMIT`, the third file after `025`/`026`: the column
  and the index land together or not at all. No guards (`IF NOT EXISTS`,
  `CREATE INDEX CONCURRENTLY`): drift must fail loudly, same reasoning as
  `024`-`026`. No `GRANT`: the new column is covered by the table's
  existing grants. Does not touch `position`, `ux_playlist_pos`,
  `playlist_tracks_reorder()` (current body in `022`) or
  `trg_playlist_tracks_reorder`. Nothing under `routes/`, `services/` or
  `models/` reads or writes `order_key` yet. Locks: the `ADD COLUMN`
  takes `ACCESS EXCLUSIVE` on `playlist_tracks` until `COMMIT`, and the
  index is built inside that same window — no deadlock risk against the
  write protocol of `013`, because this file never touches `playlists`.
  This is a normal migration (#133): it applies to the live database and
  also runs when building a new database from `017` onwards, after `026`.

  Before applying, check for drift. Four queries.

  (1) Columns, comparable with `017` lines 1320-1327:

  ```
  select column_name, data_type, collation_name, is_nullable, column_default
  from information_schema.columns
  where table_schema = 'public' and table_name = 'playlist_tracks'
  order by ordinal_position;
  ```

  should return exactly 6 rows: `playlist_id` uuid NO, `track_id` uuid
  NO, `position` integer NO, `added_by` uuid YES, `added_at` timestamp
  with time zone YES `now()`, `id` uuid NO `gen_random_uuid()`, with
  `collation_name` `NULL` on all six.

  (2) Indexes, comparable with `017` lines 1628, 1780, 1788 and 1921:

  ```
  select indexname, indexdef
  from pg_indexes
  where schemaname = 'public' and tablename = 'playlist_tracks'
  order by indexname;
  ```

  should return exactly 4 rows: `idx_playlisttracks_playlist_pos`
  (`CREATE INDEX ... USING btree (playlist_id, "position")`),
  `playlist_tracks_pkey` (`CREATE UNIQUE INDEX ... USING btree (id)`),
  `ux_playlist_pos` (`CREATE UNIQUE INDEX ... USING btree (playlist_id,
  "position")`) and `ux_playlist_track` (`CREATE UNIQUE INDEX ... USING
  btree (playlist_id, track_id)`).

  (3) Range, against the backfill's `N`:

  ```
  select min(position) as min_pos, max(position) as max_pos
  from public.playlist_tracks;
  ```

  should return `min_pos >= 1` and `max_pos <= 10000` — the `N` of
  `db/backfills/playlist_tracks_order_key.sql` (the owner measured
  `max_pos = 251` on 2026-09-23). If `max_pos > 10000`, raise `N` in
  `scripts/generate_playlist_tracks_order_key_backfill.py`, regenerate
  the backfill and run `pytest` before continuing — no new numbered file
  is needed for that, because the backfill is not a migration.

  (4) Density, informational only — does not gate applying `027` or
  running the backfill. `remove_playlist_track` (current body in `017`)
  deletes without renumbering, and `trg_playlist_tracks_reorder` is
  disabled live, so `position` can have gaps between one removal and the
  next `move_playlist_track` call on that playlist (which does renumber
  the whole playlist densely as a side effect of its own swap). Neither
  gap matters here: the key generated for position `p` is increasing in
  `p` regardless of what other positions exist, so a gap preserves the
  order; the backfill's `DO` block (`db/backfills/README.md`) compares
  orders, not position values. The only real limit is (3),
  `max(position) <= N`.

  ```
  select count(*)
  from (
    select playlist_id
    from public.playlist_tracks
    group by playlist_id
    having min(position) <> 1 or max(position) <> count(*)
  ) t;
  ```

  a non-zero result means some playlist has a gap or does not start at 1
  — worth noting, not a reason to stop.

  Any other result in (1)-(3) is drift to report as a new finding, same
  criterion as `021`-`026`, and `027` does not apply over it.

  After applying, verify with (1) and (2) again, plus one more query.
  (1) now returns 7 rows, the seventh `order_key` text `C` collation YES
  `NULL` default. (2) now returns 5 rows, the 4 above plus `CREATE INDEX
  idx_playlist_tracks_playlist_order_key ON public.playlist_tracks USING
  btree (playlist_id, order_key, id)`. And:

  ```
  select count(*) from public.playlist_tracks where order_key is not null;
  ```

  should return `0` — the column is added `NULL` for every existing row.

  `order_key` is filled in by `db/backfills/playlist_tracks_order_key.sql`,
  which needs `027` applied first. Its instructions, the check to run
  before every run, and the verification to run after (the checks for
  criteria 3 and 4 of #133) live in `db/backfills/README.md`, not here.
  Since `029` (#137) the backfill and that section of
  `db/backfills/README.md` are deleted; both are in git history at
  `2bec7e9`.
- `028_use_playlist_tracks_order_key.sql` — makes `order_key` `NOT NULL`,
  replaces its non-unique index with a unique one, and switches
  `add_playlist_track`, `add_playlist_tracks_bulk` and
  `move_playlist_track` to write it (#135, stage 2 of 3). Requires
  `db/backfills/playlist_tracks_order_key.sql` to have just been re-run
  with no write to `playlist_tracks` in between, checked before applying
  below. Adding a parameter is not something `CREATE OR REPLACE FUNCTION`
  can do across a different argument list, so the three writers are each
  `DROP FUNCTION` on the current 3-argument signature (`022` for
  add/move, `017` for bulk) followed by `CREATE FUNCTION` on the new
  4-argument one. `DROP` + `CREATE` resets privileges: `PUBLIC` gets its
  default `EXECUTE` back, and the default privileges `017` sets (line
  3111) hand `anon` an explicit grant back too. For add and bulk that
  matches what `017` already applies (lines 2667-2678: `anon`,
  `authenticated`, `service_role`, no `REVOKE FROM PUBLIC`), so nothing
  is redone. `move_playlist_track` is `REVOKE ALL ... FROM PUBLIC` with
  no grant to `anon` (`017` lines 2838-2840, finding 7) and `SECURITY
  DEFINER`, so this file repeats `REVOKE ALL ... FROM PUBLIC` and `FROM
  anon` on it right after its `CREATE`, closing the same hole finding 7
  already closed once. `get_user_playlist_thumbnails` keeps its
  signature, so it stays a plain `CREATE OR REPLACE` (`ORDER BY
  position` → `ORDER BY order_key` inside the `ROW_NUMBER()` window) and
  keeps its ACL, same reasoning as `023`. Each of the three writers also
  gains a guard, under the same lock, that the `order_key` it received
  falls strictly between its real neighbours; a guard that fails raises
  an exception tagged with the SQLSTATE and `CONSTRAINT` name a real
  violation of the new unique index would carry (`RAISE ... USING
  ERRCODE = 'unique_violation', CONSTRAINT = 'ux_playlist_order_key'`),
  so it is caught by the very same `EXCEPTION` branch a genuine index
  violation is — one handler for "collides" and "is merely out of
  order". That branch answers `{"ok": false, "error":
  "order_key_conflict"}` in the RPC's own envelope (`error`, not
  `reason` — same key `add_playlist_track` already uses for
  `track_already_in_playlist`); any other error keeps following its
  current path. `position`, `ux_playlist_pos`,
  `playlist_tracks_reorder()` (current body in `022`) and
  `trg_playlist_tracks_reorder` are untouched — dropping the trigger
  together with `position` is stage 3 (see
  `docs/adr/008-order-key-write-path.md`). Wrapped in `BEGIN`/`COMMIT`. Order inside the file, and why: (1) a `DO`
  block asserts, per playlist, that the order by `order_key` matches the
  order by `position` — the same query the backfill's own "After
  running" check runs (criterion 4 of #133) — and aborts the whole
  transaction if not, because a move landed in the window between the
  backfill's last run and this file can desynchronize the two orders
  without ever leaving a row `NULL`; (2) `ALTER COLUMN order_key SET NOT
  NULL`, which catches the other half of that same window — a row
  *added* (not moved) after the backfill's last run; (3) `DROP INDEX`
  `idx_playlist_tracks_playlist_order_key` (`027`) and `CREATE UNIQUE
  INDEX ux_playlist_order_key ON (playlist_id, order_key)` in its place
  — same prefix as the table's two other unique indexes, `ux_playlist_pos`
  and `ux_playlist_track` (`017` lines 1780, 1788) — immediate, not
  `DEFERRABLE`, so a duplicate or `NULL` key fails the `CREATE UNIQUE
  INDEX` itself, inside this transaction, rather than surfacing later as
  a deferred violation the four functions below could never catch by
  `CONSTRAINT_NAME`; (4) the three `DROP` + `CREATE FUNCTION` pairs plus
  the `REVOKE`s on `move_playlist_track`; (5) the `CREATE OR REPLACE` of
  `get_user_playlist_thumbnails`. No guards (`IF EXISTS`, `IF NOT
  EXISTS`, `CONCURRENTLY`) anywhere: drift must fail loudly, same
  reasoning as `024`-`027`. Locks: `ALTER COLUMN ... SET NOT NULL` and
  `CREATE (UNIQUE) INDEX` (no `CONCURRENTLY`) each take `ACCESS
  EXCLUSIVE` on `playlist_tracks` until `COMMIT`; this file never
  touches `playlists`, so there is no reverse lock order against the
  write protocol of `013`. This is a normal migration: it applies to the
  live database and also runs when building a new database from `017`
  onwards, after `027`.

  Before applying, check for drift. Five queries.

  (a) The `prosrc` md5 of the four functions, `\r` stripped, with the
  `extract` recipe already written above in the `022` entry, pointed at
  `022` (add, move) and `017` (bulk, thumbnails):

  ```sh
  extract() { awk -v fn="FUNCTION public.$2(" 'index($0, fn) && /^CREATE/ {hit=1; next} hit && /AS \$(function)?\$/ {body=1; next} body && /^\$(function)?\$;/ {exit} body {print}' "$1"; }
  printf 'add_playlist_track  ';          { printf '\n'; extract db/migrations/022_translate_function_body_comments.sql add_playlist_track | tr -d '\r'; } | md5 -q   # Linux: | md5sum | cut -d' ' -f1
  printf 'add_playlist_tracks_bulk  ';    { printf '\n'; extract db/migrations/017_schema_baseline.sql add_playlist_tracks_bulk | tr -d '\r'; } | md5 -q
  printf 'move_playlist_track  ';         { printf '\n'; extract db/migrations/022_translate_function_body_comments.sql move_playlist_track | tr -d '\r'; } | md5 -q
  printf 'get_user_playlist_thumbnails  '; { printf '\n'; extract db/migrations/017_schema_baseline.sql get_user_playlist_thumbnails | tr -d '\r'; } | md5 -q
  ```

  should return, in that order, `2e3ddcc187a6ff31763ac29800ca6472`,
  `8d0d6aa995559a9eccf4dada03ec6c89`, `9d8cee730a34ea41e5600525a62e6bef`,
  `50a10757d9c77805a66ae1d041ed4b7c` — the first and third match the
  post-`022` values the `022` entry above already publishes, a free
  cross-check.

  (b) Signature and privileges per role, comparing by `oid` the same way
  `025` does, plus the argument list and whether `PUBLIC` itself has
  `EXECUTE` (via `aclexplode`, since `has_function_privilege` for a role
  that never appears in `proacl` still returns true for `PUBLIC`'s own
  grantee row, not for an arbitrary role inheriting it):

  ```sql
  select p.proname,
         pg_get_function_identity_arguments(p.oid) as args,
         has_function_privilege('anon', p.oid, 'EXECUTE') as anon,
         has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated,
         has_function_privilege('service_role', p.oid, 'EXECUTE') as service_role,
         exists(
           select 1 from aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
           where a.grantee = 0
         ) as public
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.proname in ('add_playlist_track', 'add_playlist_tracks_bulk', 'move_playlist_track', 'get_user_playlist_thumbnails')
  order by p.proname;
  ```

  should return exactly 4 rows (no overload of any of the four): `add_playlist_track`
  and `add_playlist_tracks_bulk` with their current 3-argument lists, `anon`/
  `authenticated`/`service_role`/`public` all `t`; `move_playlist_track` with its
  current 3-argument list, `f`/`t`/`t`/`f`; `get_user_playlist_thumbnails` with
  `playlist_ids uuid[], limit_per_playlist integer`, `t`/`t`/`t`/`t`.

  (c) Indexes on `playlist_tracks`, same query as the `027` entry: should
  return the same 5 rows `027` leaves — `idx_playlisttracks_playlist_pos`,
  `idx_playlist_tracks_playlist_order_key`, `playlist_tracks_pkey`,
  `ux_playlist_pos`, `ux_playlist_track`.

  (d) `trg_playlist_tracks_reorder` and `trg_bump_playlist_on_track_change`,
  query (1) of `db/backfills/README.md`: `trg_playlist_tracks_reorder` in
  `D`, `trg_bump_playlist_on_track_change` in `O`.

  (e) The backfill has just been run, and its "After running" section
  (`db/backfills/README.md`) gives `null_keys = 0`, 0 mismatches and 0
  duplicate keys. This is mandatory, not informational — without it,
  step (1) of this file's own `DO` block, or the `SET NOT NULL` right
  after it, is what would catch the drift instead, but at the cost of
  the whole transaction rolling back after doing real work.

  If any of (a)-(d) does not return exactly what is described above, or
  (e) was not just confirmed, that is drift (or a missing precondition)
  to report as a new finding, and `028` does not apply over it.

  After applying, verify:

  (b) again, pointed at the same four functions: still exactly 4 rows,
  now with `args` including `p_order_key text` (add, move) or
  `p_order_keys text[]` (bulk), and the same `anon`/`authenticated`/
  `service_role`/`public` booleans per function as before applying —
  the `DROP` + `CREATE` resets and this file's own `REVOKE`s on
  `move_playlist_track` land it back on the same four values.

  (c) again: 5 rows, `idx_playlist_tracks_playlist_order_key` replaced by
  `ux_playlist_order_key` (`CREATE UNIQUE INDEX ux_playlist_order_key ON
  public.playlist_tracks USING btree (playlist_id, order_key)`), the
  other 4 unchanged.

  ```sql
  select is_nullable
  from information_schema.columns
  where table_schema = 'public' and table_name = 'playlist_tracks' and column_name = 'order_key';
  ```

  should return `NO`.

  ```sql
  select count(*) from public.playlist_tracks where order_key is null;
  ```

  should return `0`.

  The md5 of `get_user_playlist_thumbnails`, same recipe as (a) pointed
  at `028` instead of `017`, should return `179afd33c417b0bea9b2963d26dd593f`
  — different from the pre-apply value, because `ORDER BY position`
  became `ORDER BY order_key`.

  (d) again: unchanged from before applying — this file's `DO` block and
  the two `ALTER TABLE`/`CREATE INDEX` statements never touch a trigger.

  **Collision check, leaving no trace** (confirms `CONSTRAINT_NAME`
  carries the index's name, on a playlist with 3 or more tracks):

  ```sql
  begin;
  select public.move_playlist_track(
    '<playlist_id>', 1, 2,
    (select order_key from public.playlist_tracks where playlist_id = '<playlist_id>' order by order_key limit 1 offset 2)
  );
  rollback;
  ```

  should return `{"ok": false, "error": "order_key_conflict"}`.

  Since `029` (#137) the backfill that checks (d) and (e) and the header
  of `028` refer to, and the `db/backfills/README.md` sections they point
  to, are deleted; they are in git history at `2bec7e9`. `028`'s own text
  stays as written.
- `029_drop_playlist_tracks_position.sql` — drops
  `public.playlist_tracks.position`, `trg_playlist_tracks_reorder`,
  `playlist_tracks_reorder()`, `ux_playlist_pos` and
  `idx_playlisttracks_playlist_pos`, and switches `add_playlist_track`,
  `add_playlist_tracks_bulk` and `move_playlist_track` to compute the
  position they return instead of reading or writing it (#137, stage 3 of
  3). Order inside the transaction, and why: (1) a `DO` block, copied
  verbatim from `028`, asserts that the order by `order_key` matches the
  order by `position` for every playlist and aborts if not — the same
  gate `028` already runs, repeated here because this is the last chance
  to catch drift before the old order is destroyed for good; (2) `DROP
  TRIGGER trg_playlist_tracks_reorder` and `DROP FUNCTION
  playlist_tracks_reorder()` — the trigger is disabled live (ADR 007) and
  its only job was keeping `position` dense, so once `position` is gone
  neither has a reader left; (3) `DROP CONSTRAINT ux_playlist_pos`, then
  `DROP INDEX idx_playlisttracks_playlist_pos`, then `DROP COLUMN
  position`, in that order and no other — `DROP COLUMN` removes any index
  or constraint on it automatically, so the explicit drops have to go
  first or they would fail with "does not exist" against an object `DROP
  COLUMN` already took with it; (4) the three `CREATE OR REPLACE
  FUNCTION`s. Unlike `028`, none of the three writers' signatures changes
  here, so this file uses `CREATE OR REPLACE FUNCTION`, not `DROP` +
  `CREATE`: it keeps every grant and revoke `028` already applied with no
  `GRANT`/`REVOKE` of its own. `CREATE OR REPLACE` does reset any
  attribute it does not restate, though, so `move_playlist_track`'s
  `SECURITY DEFINER` and `SET search_path TO 'public', 'pg_temp'` are
  copied verbatim — omitting either would silently drop the function back
  to `SECURITY INVOKER` or an unpinned `search_path`.

  The repo owner decided the API keeps returning `position` in the JSON
  key `add_playlist_track` and `move_playlist_track` already used, but
  computed rather than stored: `add_playlist_track` takes a `SELECT
  COUNT(*)` under the same playlist lock right after its insert, and
  `move_playlist_track`'s two `order` subqueries use `ROW_NUMBER() OVER
  (ORDER BY order_key)` in place of the dropped column. See
  `docs/api/playlists.md` and `docs/adr/009-drop-playlist-tracks-position.md`.

  Order of application: this file can only be applied once the PR for
  #137 is merged and the backend is running that code — the old code
  reads `"track_id, position"` from `playlist_tracks` and gets a 502 the
  moment this file drops the column. Once the PR is merged, this file can
  be applied at any point after that; nothing here depends on when.
  Between the merge and applying this file, `POST
  /playlists/{playlist_id}/tracks` can return a `position` larger than
  the index `GET /playlists/{playlist_id}` gives that same track, in a
  playlist with gaps in the old `position` column (finding 3): the add
  RPC is still `028`'s, answering the stored `MAX(position) + 1`, while
  the code already reads the computed index. This is a real, documented
  gap, not a bug to work around — it closes the moment this file is
  applied. See `docs/adr/009-drop-playlist-tracks-position.md`.

  Locks: `DROP TRIGGER` takes `SHARE ROW EXCLUSIVE` on `playlist_tracks`;
  `ALTER TABLE ... DROP CONSTRAINT`, `DROP INDEX` and `ALTER TABLE ...
  DROP COLUMN` each take `ACCESS EXCLUSIVE` on `playlist_tracks` until
  `COMMIT`. This file never touches `playlists`, so there is no reverse
  lock order against the write protocol of `013`.

  Untouched: `remove_playlist_track` (current body in `017`),
  `get_user_playlist_thumbnails` (current body in `028`, already orders
  by `order_key`), `get_playlist_thumbnails`,
  `recommend_playlists_by_history` (both current body in `023`, both read
  `genre_playlist_tracks`, a different table with its own `position`
  column this file does not touch) and `genre_playlist_tracks` itself.
  This is a normal migration: it applies to the live database and also
  runs when building a new database from `017` onwards, after `028`.

  Before applying, check for drift.

  (a) The `prosrc` md5 of the three writers, `\r` stripped, with the
  `extract` recipe already written above in the `022` entry, pointed at
  `028`, plus `playlist_tracks_reorder`, current body in `022`:

  ```sh
  extract() { awk -v fn="FUNCTION public.$2(" 'index($0, fn) && /^CREATE/ {hit=1; next} hit && /AS \$(function)?\$/ {body=1; next} body && /^\$(function)?\$;/ {exit} body {print}' "$1"; }
  printf 'add_playlist_track  ';         { printf '\n'; extract db/migrations/028_use_playlist_tracks_order_key.sql add_playlist_track | tr -d '\r'; } | md5 -q   # Linux: | md5sum | cut -d' ' -f1
  printf 'add_playlist_tracks_bulk  ';   { printf '\n'; extract db/migrations/028_use_playlist_tracks_order_key.sql add_playlist_tracks_bulk | tr -d '\r'; } | md5 -q
  printf 'move_playlist_track  ';        { printf '\n'; extract db/migrations/028_use_playlist_tracks_order_key.sql move_playlist_track | tr -d '\r'; } | md5 -q
  printf 'get_user_playlist_thumbnails  '; { printf '\n'; extract db/migrations/028_use_playlist_tracks_order_key.sql get_user_playlist_thumbnails | tr -d '\r'; } | md5 -q
  printf 'playlist_tracks_reorder  ';    { printf '\n'; extract db/migrations/022_translate_function_body_comments.sql playlist_tracks_reorder | tr -d '\r'; } | md5 -q
  ```

  should return, in that order: `1cde77d2753cc8296c2849788c41873e`,
  `756ae10724f5d2fe0fe6f7fe772a6047`, `ef52cb3a9139f82371e6f7ca41c8c462`,
  `179afd33c417b0bea9b2963d26dd593f`, `43cbfc771352861b14e34d141636f2d3` —
  the first three were not published by any earlier entry (`028` never
  published its own "after applying" values for these three), the fourth
  matches `028`'s and the fifth matches `022`'s, a free cross-check for
  those two only.

  (b) Signature and privileges, same query as the `028` entry pointed at
  the same four functions plus the argument list check: should return the
  same 4 rows `028`'s own "after applying" leaves — unchanged, because
  this file's `CREATE OR REPLACE` does not touch privileges.

  (c) Indexes on `playlist_tracks`, same query as the `027`/`028` entries:
  should return the same 5 rows `028` leaves —
  `idx_playlisttracks_playlist_pos`, `playlist_tracks_pkey`,
  `ux_playlist_order_key`, `ux_playlist_pos`, `ux_playlist_track`.

  (d) Triggers, the query `db/backfills/README.md` used to carry before
  `029` deleted that section (#137):

  ```
  set search_path to '';
  select t.tgname, t.tgenabled, pg_catalog.pg_get_triggerdef(t.oid) as def
  from pg_catalog.pg_trigger t
  where t.tgrelid = 'public.playlist_tracks'::regclass
    and not t.tgisinternal
  order by t.tgname;
  ```

  should return exactly 2 rows: `trg_bump_playlist_on_track_change` `O`,
  `trg_playlist_tracks_reorder` `D`. If a row comes back without
  `public.` qualifying the function in `def`, the `set` did not take in
  that session — run the two statements together; that alone is not
  drift, same note as `025`/`026`.

  (e) Columns, same query as the `027` entry: should return the same 7
  rows `028` leaves, including `position` integer NO and `order_key` text
  `C` NO.

  (f) Constraints:

  ```sql
  select conname, contype, pg_get_constraintdef(oid)
  from pg_constraint
  where conrelid = 'public.playlist_tracks'::regclass and contype in ('p', 'u', 'f')
  order by conname;
  ```

  should return exactly 6 rows: `playlist_tracks_added_by_fkey`,
  `playlist_tracks_pkey`, `playlist_tracks_playlist_id_fkey`,
  `playlist_tracks_track_id_fkey`, `ux_playlist_pos` (`UNIQUE (playlist_id,
  "position") DEFERRABLE INITIALLY DEFERRED`), `ux_playlist_track` (`017`
  lines 1628, 1780, 1788, 2226, 2234, 2242). Filtering by `contype` avoids
  depending on whether the Postgres version lists `NOT NULL` as a
  constraint.

  (g) OBLIGATORY, immediately before applying: the mismatch query from
  `028`'s own check (a) (order by `position` vs. by `order_key COLLATE
  "C"`) must return `0`. This file's own `DO` block repeats it inside the
  transaction, but a failure there is a failure after locks are already
  held.

  (h) Attributes:

  ```sql
  select proname, prosecdef, provolatile, proconfig
  from pg_proc
  where pronamespace = 'public'::regnamespace
    and proname in ('add_playlist_track', 'add_playlist_tracks_bulk', 'move_playlist_track')
  order by proname;
  ```

  should return exactly 3 rows: `move_playlist_track` `t` with
  `{"search_path=public, pg_temp"}`, the other two `f` and `NULL`, all
  three `v`.

  (i) OBLIGATORY: the PR for #137 is merged into `main`, the backend
  running locally is on that `main` (or a later one), and `GET
  /playlists/{playlist_id}` responds 200 against the database still on
  `028`. With code from before that merge, applying this file leaves `GET
  /playlists/{playlist_id}` and `GET /public/playlists/{playlist_id}` on
  502.

  Any other result is drift to report as a new finding, and `029` does
  not apply over it.

  After applying, verify:

  (a) again, pointed at `029` instead of `028`/`022`: `add_playlist_track`
  `69fb4ba0a5971ce8855a90ff8ef7c754`, `add_playlist_tracks_bulk`
  `2b673cad49ace28b50c25ed353469425`, `move_playlist_track`
  `0369dacb87648e2f13d31e5efd966126`, `get_user_playlist_thumbnails`
  unchanged (`179afd33c417b0bea9b2963d26dd593f` — this file does not
  redefine it), and `select count(*) from pg_proc where pronamespace =
  'public'::regnamespace and proname = 'playlist_tracks_reorder';` = `0`
  — `playlist_tracks_reorder` no longer exists to md5.

  (b) again: the same 4 rows, unchanged — `CREATE OR REPLACE` keeps the
  ACL.

  (c) again: 3 rows, `playlist_tracks_pkey`, `ux_playlist_order_key`,
  `ux_playlist_track`.

  (d) again: 1 row, `trg_bump_playlist_on_track_change` `O`.

  (e) again: 6 rows, the 7 of `028` minus `position`.

  (f) again: 5 rows, the 6 above minus `ux_playlist_pos`.

  (h) again: unchanged.

  **QA, leaving no trace where noted:**

  Collision, same shape as `028`'s own check: `begin; select
  public.move_playlist_track('<playlist_id>', 1, 2, <an order_key already
  in use in that playlist>); rollback;` should return `{"ok": false,
  "error": "order_key_conflict"}`.

  A real move touches exactly one row:

  ```sql
  begin;
  select public.move_playlist_track('<playlist_id>', 1, 3, <a valid new order_key>);
  select count(*) from public.playlist_tracks
  where playlist_id = '<playlist_id>' and xmin::text = txid_current()::text;
  rollback;
  ```

  should return `1`. NOT PROVEN reliable: the owner has to confirm
  `xmin` behaves this way on their instance, and fall back to a
  `pg_stat_statements`/log-based check if it does not.

  Add's `position` matches the GET's index, without a trace:

  ```sql
  begin;
  select public.add_playlist_track(
    '<playlist_id>', '<tracks.id not already in the playlist>', '<uuid of auth.users>',
    (select max(order_key) || 'z' from public.playlist_tracks where playlist_id = '<playlist_id>')
  );
  select rn, total
  from (
    select id,
      row_number() over (order by order_key) as rn,
      count(*) over () as total
    from public.playlist_tracks
    where playlist_id = '<playlist_id>'
  ) t
  where id = '<id from the JSON above>';
  rollback;
  ```

  The first `select` should return `{"ok": true, "id": ..., "position":
  P}` and the second `rn = P` and `total = P`. `max(order_key) || 'z'` is
  strictly greater under collation `C` (a string always sorts before any
  extension of itself); it is not a "well-formed" key for the library,
  but the `rollback` discards it. NOT PROVEN: not run against any
  Postgres.

  Add's `position` matches the GET's index, through the API: `POST
  /playlists/{playlist_id}/tracks` on a playlist that had gaps in the old
  `position`, then immediately walk `GET
  /playlists/{playlist_id}/tracks` to its last page — the `position` the
  POST returned should equal that track's `position` in `data.items`
  (the new track is the last one), the first page's `data.page.total`,
  and `GET /playlists/{playlist_id}`'s `total_count`. Rewritten for
  #139: before it, `GET /playlists/{playlist_id}` carried the tracks
  inline in `data.tracks`; since #139 it returns no tracks, and they
  come from the paginated endpoint.
- `030_owner_write_policies_for_user_client.sql` — adds the INSERT policy
  `play_events` and `recent_activity` were missing, the UPDATE policy
  `recent_activity` was missing, and widens `bug_reports`' INSERT policy
  from testers-and-above to any authenticated caller inserting their own
  report — the three writes the six user-data domains' authenticated
  routes now make with the user-scoped Supabase client instead of
  service_role (#143), which makes RLS apply to these writes for the
  first time and requires policies that let them through. `TO
  authenticated` and USING-only on the UPDATE match
  every owner policy on a user-data table already in `017`; DROP + CREATE
  on `bug_reports`, not `ALTER POLICY`, because the WITH CHECK expression
  itself changes, not just the policy's roles. Full reasoning in the
  file's own header. This is a normal migration: it applies to the live
  database and also runs when building a new database from `017`
  onwards, after `029`.

  Before applying, check for drift. Three queries.

  Query 1 (policies):

  ```
  set search_path to '';
  select tablename, policyname, permissive, roles, cmd, qual, with_check
  from pg_catalog.pg_policies
  where schemaname = 'public' and tablename in ('bug_reports', 'play_events', 'recent_activity')
  order by tablename, policyname;
  ```

  should return exactly 7 rows, all `PERMISSIVE`:

  | tablename | policyname | roles | cmd | qual | with_check |
  |---|---|---|---|---|---|
  | bug_reports | Admins can delete reports | {authenticated} | DELETE | public.is_admin() | NULL |
  | bug_reports | Developers and admins can update reports | {authenticated} | UPDATE | public.is_developer_or_higher() | NULL |
  | bug_reports | Developers and admins can view all reports | {authenticated} | SELECT | public.is_developer_or_higher() | NULL |
  | bug_reports | Testers and above can create reports | {authenticated} | INSERT | NULL | ((auth.uid() = reporter_id) AND public.is_tester_or_higher()) |
  | bug_reports | Users can view own reports | {public} | SELECT | (auth.uid() = reporter_id) | NULL |
  | play_events | play_events readable by owner | {authenticated} | SELECT | (user_id = auth.uid()) | NULL |
  | recent_activity | recent_activity readable by owner | {authenticated} | SELECT | (user_id = auth.uid()) | NULL |

  (`bug_reports` per `017` + `025`; the other two per `017` lines 2473
  and 2552.) A row already present for INSERT or UPDATE on
  `play_events`/`recent_activity` (for example, added by hand because a
  mobile client writes directly) is drift: `030` does not apply over it,
  report it as a new finding.

  Query 2 (RLS active):

  ```
  select relname, relrowsecurity, relforcerowsecurity
  from pg_catalog.pg_class
  where oid in ('public.bug_reports'::regclass, 'public.play_events'::regclass, 'public.recent_activity'::regclass)
  order by relname;
  ```

  should return 3 rows, `relrowsecurity = t`, `relforcerowsecurity = f`
  on all three (`017` lines 2376, 2467, 2546; `017` has no `FORCE` on any
  of the three).

  Query 3 (callers): the `(a)` query of the `025` entry above, unchanged
  — should return the same 6 rows that entry describes.

  Any other result on any of the three queries is drift to report as a
  new finding, and `030` does not apply over it.

  After applying, verify with the same three queries. Query 1 -> 9 rows:
  `"Testers and above can create reports"` is gone; four rows are new —
  `"Users can create own reports"` (`bug_reports`, `{authenticated}`,
  INSERT, `qual` NULL, `with_check` `(auth.uid() = reporter_id)`),
  `"play_events insertable by owner"` (`{authenticated}`, INSERT, NULL,
  `(user_id = auth.uid())`), `"recent_activity insertable by owner"`
  (same shape) and `"recent_activity updatable by owner"`
  (`{authenticated}`, UPDATE, `(user_id = auth.uid())`, NULL); the other
  6 rows unchanged. Query 2: unchanged. Query 3 -> 5 rows: the row for
  `"Testers and above can create reports"` is gone — `is_tester_or_higher()`
  is left with no policy calling it any more; the function itself is not
  dropped, that is a separate change.
- `031_cleanup_library_on_playlist_delete_security_definer.sql` — makes
  `cleanup_library_on_playlist_delete()` (the body of the AFTER DELETE
  trigger on `playlists` that removes the deleted playlist's rows from
  every saver's `library_items`) `SECURITY DEFINER`, with a pinned
  `search_path` and `EXECUTE` revoked from `PUBLIC`/`anon`, restoring its
  pre-`#143` reach. The function is `SECURITY INVOKER` (the default) in
  `017`; its cleanup is meant to remove every saver's `library_items` row
  for a deleted playlist, not just the deleting user's own, and that is
  how it behaved under `service_role`, which runs unfiltered by RLS.
  Since `#143`, `DELETE /playlists/{playlist_id}` runs as `authenticated`
  with the deleting user's own JWT instead of `service_role`, so the
  function's own `DELETE FROM public.library_items` is now filtered by
  `"library_items deletable by owner"` to the caller's own rows, which is
  narrower than the cleanup is meant to be. `SECURITY DEFINER` makes the
  function run as its owner, independent of RLS the same way
  `service_role` used to be, keeping the cleanup's original, all-users
  reach (P1(a), `plan-143` addendum). The body
  is copied verbatim from `017` lines 356-367 (`\r` stripped; see the
  file's own header and the md5 check below), `SET search_path TO
  'public', 'pg_temp'` matches `018`/`021`'s pinning of every other
  `SECURITY DEFINER` function, and the two `REVOKE`s match the five `025`
  already closed — same reasoning, repeated in the file's own header, not
  here. No `GRANT`: `CREATE OR REPLACE` preserves the existing ACL, so
  `authenticated` and `service_role` keep their grant. The trigger
  binding is unaffected: `CREATE OR REPLACE FUNCTION` keeps the name and
  signature, and `EXECUTE` on a trigger function is required from
  whoever runs `CREATE TRIGGER`, not from whoever fires it. This is a
  normal migration: it applies to the live database and also runs when
  building a new database from `017` onwards, after `030`.

  Before applying, check for drift. Three queries.

  Query 1 (body):

  ```sql
  select md5(replace(prosrc, E'\r', '')) from pg_proc where oid = 'public.cleanup_library_on_playlist_delete()'::regprocedure;
  ```

  should return `1451cb68940aa95880c7766192ea6aa9`, the same md5 the
  `021` entry's recipe gives for `017`'s body of this function (`extract`
  + `printf '\n'` + `tr -d '\r'` + `md5 -q`, cited there, not repeated
  here).

  Query 2 (security and ACL):

  ```sql
  select p.proname, p.prosecdef, p.proconfig,
         has_function_privilege('anon', p.oid, 'EXECUTE') as anon,
         has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated,
         has_function_privilege('service_role', p.oid, 'EXECUTE') as service_role
  from pg_proc p
  where p.oid = 'public.cleanup_library_on_playlist_delete()'::regprocedure;
  ```

  should return 1 row: `prosecdef = f`, `proconfig = NULL`, `anon = t`,
  `authenticated = t`, `service_role = t` (`017` lines 356-369 and
  2712-2714).

  Query 3 (binding):

  ```sql
  select tgname, tgenabled from pg_trigger where tgfoid = 'public.cleanup_library_on_playlist_delete()'::regprocedure;
  ```

  should return 1 row: `trg_cleanup_library_on_playlist_delete`,
  `tgenabled = O`.

  Any other result on any of the three queries is drift to report as a
  new finding, and `031` does not apply over it.

  After applying, verify with the same three queries. Query 1: the same
  md5 — the body does not change, only `prosrc` moving from CRLF to LF,
  which `replace` already normalized before hashing. Query 2: `prosecdef
  = t`, `proconfig = {"search_path=public, pg_temp"}`, `anon = f`,
  `authenticated = t`, `service_role = t`. Query 3: unchanged.

  Whether `library_items` has, today, any row with `kind = 'playlist' AND
  source = 'external'` whose `user_id` is not the deleted playlist's
  owner (which would show how much the pre-`#143` version of this trigger
  used to clean up beyond the caller's own row) was not queried live.
  Left for the repo owner to check if they want to; it does not block
  this file, which restores the pre-`#143` behavior regardless of the
  answer.
- `032_restrict_aggregation_helpers_to_service_role.sql` — revokes
  `EXECUTE` from `authenticated` on `get_active_users_in_period(p_start
  date, p_end date)` and `get_users_with_weekly_stats(p_weeks date[])`,
  leaving `service_role` as the only role that can run them (#146). Both
  are `SECURITY DEFINER` (`017` lines 375-376 and 573-574, current body
  since `021`), so they run as `postgres`, read `play_events` /
  `user_weekly_stats` without filtering by caller, and return user ids
  for the whole period — any `authenticated` caller could use them to
  bypass the per-owner policies on those two tables. Their ACL in `017`
  (lines 2721-2723 and 2784-2786) already does `REVOKE ALL ... FROM
  PUBLIC`, with `GRANT ALL` only to `authenticated` and to `service_role`
  — no `GRANT ... TO anon` — so unlike the five functions `025` closed,
  this file needs only one `REVOKE` line per function (`FROM
  authenticated`); `PUBLIC` and `anon` are not touched. No `GRANT`:
  `service_role` keeps the grant it already has from `017`. No `CREATE OR
  REPLACE`: neither body, signature, volatility, `SECURITY DEFINER` nor
  `search_path` changes for either function. Same warning as `025`/`031`:
  a future `DROP` + `CREATE FUNCTION` on either of these two resets its
  ACL, and the default privileges `017` sets (lines 3111-3112, `GRANT ALL
  ON FUNCTIONS TO anon` and `TO authenticated`) hand `EXECUTE` back to
  both `anon` and `authenticated`; both the `REVOKE ... FROM PUBLIC` from
  `017` and the `REVOKE` below would need to be repeated. `BEGIN`/`COMMIT`,
  same as every file that has changed privileges on the live database
  since `025`: both functions end up revoked, or neither does. Nobody
  calls them: not the backend (`services/`, `routes/`, 0 hits), not
  another function, policy or view in `public` per `017`, and not the one
  live cron job (`purge-old-data` -> `purge_old_data()`). This is a
  normal migration: it applies to the live database and also runs when
  building a new database, after `031`.

  `025`'s header and entry pair `017`'s line numbers with these names in
  a different order; the correct lines are `get_active_users_in_period`
  2721, `get_users_with_weekly_stats` 2784, `move_playlist_track` 2838
  (`025` itself is not edited).

  Before applying, check for drift. Three queries.

  Query 1 (privileges and security, `031`'s query 2 shape):

  ```sql
  select p.oid::regprocedure as function, p.prosecdef, p.proconfig,
         has_function_privilege('public', p.oid, 'EXECUTE') as public,
         has_function_privilege('anon', p.oid, 'EXECUTE') as anon,
         has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated,
         has_function_privilege('service_role', p.oid, 'EXECUTE') as service_role
  from pg_proc p
  where p.oid in ('public.get_active_users_in_period(date, date)'::regprocedure,
                  'public.get_users_with_weekly_stats(date[])'::regprocedure)
  order by 1;
  ```

  should return 2 rows, both `prosecdef = t`, `proconfig =
  {"search_path=public, pg_temp"}` (`021`), `public = f`, `anon = f`,
  `authenticated = t`, `service_role = t` (`017` lines 2721-2723 and
  2784-2786). If the `regprocedure` cast fails because the signature does
  not exist, that is also drift.

  Query 2 (callers across the whole catalog, not only `public`, the (a)
  and (b) shape from the `025` entry above):

  (a)

  ```sql
  select d.classid::regclass as catalog,
         pg_catalog.pg_describe_object(d.classid, d.objid, d.objsubid) as dependent,
         d.refobjid::regprocedure as helper
  from pg_catalog.pg_depend d
  where d.refclassid = 'pg_catalog.pg_proc'::regclass
    and d.refobjid in ('public.get_active_users_in_period(date, date)'::regprocedure,
                       'public.get_users_with_weekly_stats(date[])'::regprocedure)
  order by 2;
  ```

  should return 0 rows — no policy, view, default or trigger depends on
  either function.

  (b)

  ```sql
  select p.oid::regprocedure as caller, p.prosecdef
  from pg_catalog.pg_proc p
  where p.prosrc ~ '\m(get_active_users_in_period|get_users_with_weekly_stats)\M'
    and p.oid not in ('public.get_active_users_in_period(date, date)'::regprocedure,
                      'public.get_users_with_weekly_stats(date[])'::regprocedure)
  order by 1;
  ```

  should return 0 rows — no other body in `017` names either function.

  Query 3 (cron):

  ```sql
  select jobname, schedule, command from cron.job order by jobname;
  ```

  should return 1 row, `purge-old-data`, `0 4 * * 0`, `select
  purge_old_data()` (this README, "INCOMPLETE" section, consulted
  2026-09-06). An extra row that calls either function is drift; an
  extra row that does not call either is worth noting but does not
  block this file.

  Any other result on any of the three queries is drift to report as a
  new finding, and `032` does not apply over it.

  After applying, verify with the same three queries. Query 1: the same
  2 rows, `authenticated = f` on both, everything else unchanged
  (`service_role = t`, `public = f`, `anon = f`, `prosecdef`/`proconfig`
  unchanged). Query 2 and Query 3: unchanged.

  Note on query 1: `has_function_privilege` resolves role inheritance,
  which is why it is used instead of comparing `proacl` as text — same
  criterion as `025`.

  Functional check, from the issue's QA checklist: `set role
  authenticated; select * from get_active_users_in_period(current_date -
  7, current_date);` and the same with `get_users_with_weekly_stats(array
  [current_date])` should both return `permission denied for function`,
  not rows; the same with `anon`, which already gave that error since
  `017`; `reset role;` after.
- `033_move_playlist_track_stop_returning_sqlerrm.sql` — the two
  `EXCEPTION` branches of `move_playlist_track` that returned `SQLERRM`
  (`WHEN unique_violation` when the constraint is not
  `ux_playlist_order_key`, and `WHEN OTHERS`) now write the detail with
  `RAISE LOG` (the message, `SQLSTATE`, `p_playlist_id`, and, in the
  first branch, `v_constraint`) and return `'internal_error'` (#148).
  `internal_error` is `AppError`'s default reason
  (`docs/api/conventions.md`) and does not collide with the function's
  own four reasons. `RAISE LOG`, not `RAISE WARNING`: with Postgres's
  default GUCs a `LOG` message reaches only the server log, while a
  `WARNING` also travels to the client — see the file's own header for
  the full reasoning on both.

  What does not change: the signature, `RETURNS json`, `LANGUAGE
  plpgsql`, volatility, `SECURITY DEFINER`, `SET search_path TO
  'public', 'pg_temp'`, the owner check, the `FOR UPDATE` lock, the
  `order_key` reorder, the `order_key_conflict` branch and the other
  three reasons; the backend does not change either — the service
  already turns any `ok: false` other than `order_key_conflict` into
  `upstream_error` — and no HTTP response changes.

  `CREATE OR REPLACE FUNCTION`, not `DROP` + `CREATE`: the signature is
  unchanged, so it keeps the ACL `028` left (`anon = f`, `authenticated
  = t`, `service_role = t`, `PUBLIC = f`) without any `GRANT`/`REVOKE`;
  same warning as `025`/`031`/`032` about a future `DROP` + `CREATE
  FUNCTION` resetting the ACL and the default privileges (`017` lines
  3111-3112) handing `EXECUTE` back to `anon`. `BEGIN`/`COMMIT`, same as
  every file that has changed a function on the live database since
  `025`. This is a normal migration: it applies to the live database and
  also runs when building a new database from `017`, after `032`.

  Verification that the body is byte-for-byte identical to `029` except
  for those two branches:

  ```sh
  S=db/migrations/029_drop_playlist_tracks_position.sql
  N=db/migrations/033_move_playlist_track_stop_returning_sqlerrm.sql
  fn() { awk -v fn="FUNCTION public.$2(" 'index($0, fn) && /^CREATE/ {hit=1} hit {print} hit && /^\$function\$;/ {exit}' "$1"; }
  E=$(mktemp)
  cat > "$E" <<'EOF2'
  107c107,108
  <     RETURN json_build_object('ok', false, 'error', SQLERRM);
  ---
  >     RAISE LOG 'move_playlist_track: unique_violation on constraint % for playlist %: % (SQLSTATE %)', v_constraint, p_playlist_id, SQLERRM, SQLSTATE;
  >     RETURN json_build_object('ok', false, 'error', 'internal_error');
  109c110,111
  <     RETURN json_build_object('ok', false, 'error', SQLERRM);
  ---
  >     RAISE LOG 'move_playlist_track: unhandled error for playlist %: % (SQLSTATE %)', p_playlist_id, SQLERRM, SQLSTATE;
  >     RETURN json_build_object('ok', false, 'error', 'internal_error');
  EOF2
  diff <(fn "$S" move_playlist_track) <(fn "$N" move_playlist_track) | cmp - "$E" && echo OK-body
  ```

  The diff against `029` is exactly those two branches: two `RETURN`
  lines changed, two `RAISE LOG` lines added.

  Before applying, check for drift. Three queries.

  Query (a) (body):

  ```sql
  select md5(replace(prosrc, E'\r', '')) from pg_proc where oid = 'public.move_playlist_track(uuid, integer, integer, text)'::regprocedure;
  ```

  should return `0369dacb87648e2f13d31e5efd966126`, the same value the
  `029` entry publishes as "After applying" for `move_playlist_track`,
  obtained with the `extract` recipe the `022` entry publishes, pointed
  at `029`. If the cast to `regprocedure` fails, that is also drift.

  Query (b) (attributes and privileges):

  ```sql
  select p.oid::regprocedure as function, p.prosecdef, p.provolatile, p.proconfig,
         has_function_privilege('anon', p.oid, 'EXECUTE') as anon,
         has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated,
         has_function_privilege('service_role', p.oid, 'EXECUTE') as service_role,
         exists(
           select 1 from aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
           where a.grantee = 0
         ) as public
  from pg_proc p
  where p.pronamespace = 'public'::regnamespace and p.proname = 'move_playlist_track';
  ```

  should return exactly 1 row, no overloads:
  `move_playlist_track(uuid,integer,integer,text)`, `prosecdef = t`,
  `provolatile = v`, `proconfig = {"search_path=public, pg_temp"}`,
  `anon = f`, `authenticated = t`, `service_role = t`, `public = f` —
  the values `028` (b) and `029` (b)/(h) leave.

  Query (c) (no other function still leaks the raw text):

  ```sql
  select p.oid::regprocedure from pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and p.prosrc ~ '''error''\s*,\s*SQLERRM';
  ```

  should return 1 row, `move_playlist_track(uuid,integer,integer,text)`.

  Any other result on any of the three queries is drift to report as a
  new finding, and `033` does not apply over it.

  After applying, verify with the same three queries. Query (a): now
  `2150b04838d98c1526a097e64c9e1204` (md5 of `033`'s body with the same
  recipe). Query (b): unchanged. Query (c): 0 rows.

  QA, without leaving a trace (NOT PROVEN: not run against any
  Postgres):

  ```sql
  begin;
  select public.move_playlist_track('<playlist_id with at least 2 tracks>', 1, 2, NULL);
  rollback;
  ```

  run as `postgres`/`service_role` (`auth.uid()` null) in the SQL
  editor: the `UPDATE ... SET order_key = NULL` violates the `NOT NULL`
  `028` put on `order_key` (23502, not `unique_violation`), falls into
  `WHEN OTHERS`, and should return `{"ok" : false, "error" :
  "internal_error"}`; before applying, the same call returns the raw
  Postgres text (`null value in column "order_key" ...`), for contrast.
  Afterwards, the Postgres logs in Supabase's Logs Explorer should show
  a `LOG` line with `move_playlist_track: unhandled error for playlist
  <playlist_id>: null value in column ... (SQLSTATE 23502)`, and the SQL
  editor's response should carry no notice with that text. The unnamed
  `unique_violation` branch has no known trigger (the function only
  writes `order_key`, covered by `ux_playlist_order_key`); it is covered
  by the diff above instead.

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
   RESOLVED 2026-09-22 by `026_unify_updated_at_trigger_functions.sql`
   (#131): `genre_playlists` now uses `update_updated_at()`, and
   `update_updated_at_column()` no longer exists in `public`. By the
   time this was resolved, `update_updated_at` was no longer bound to
   `upcoming_releases` alone: `015` and `016` had already bound it to
   `user_likes`, `library_items` and `bug_reports` too, so `026` leaves
   a single function serving all five tables. This was unified on the
   live database, not only "when building the new database" — `026`
   is a normal migration and runs on both paths.
   `storage.update_updated_at_column` is a Supabase-managed function on
   a different schema and is not part of this finding.
2. `get_playlist_thumbnails` (genre) and `get_user_playlist_thumbnails`
   (user) have near-identical names and different tables — renaming is
   optional, not mixing them up is mandatory.
3. RESOLVED 2026-07-28: `playlist_tracks_reorder` is an ACTIVE trigger
   (`trg_playlist_tracks_reorder`), not dead legacy — it maintains position
   contiguity on delete. `move_track_position` and `update_positions` were
   dead code (no callers, legacy app retired) — dropped in 010.
   CORRECTED 2026-09-23 (#135): the "ACTIVE" premise above is wrong.
   `trg_playlist_tracks_reorder` is disabled live (`tgenabled = D`, `017`
   line 2133, measured again on 2026-09-23, ADR 007) and nothing in
   `db/migrations/` or `db/backfills/` re-enables it. `remove_playlist_track`
   (current body in `017`) deletes without renumbering, so `position` can
   have gaps between one removal and the next `move_playlist_track` call on
   that playlist — contiguity is not maintained by anyone. Dropping the
   trigger together with `position` is deferred to the third stage of the
   order-key migration (see `docs/adr/008-order-key-write-path.md`), not
   done here.
   RESOLVED 2026-09-24 by `029_drop_playlist_tracks_position.sql` (#137):
   `trg_playlist_tracks_reorder`, `playlist_tracks_reorder()`,
   `ux_playlist_pos`, `idx_playlisttracks_playlist_pos` and the column
   `position` no longer exist; the "third stage" this `CORRECTED
   2026-09-23` note defers is this file.
4. `move_playlist_track` returns `SQLERRM` in the `error` field of its
   JSON; the service surfaces it as `upstream_error` and it never reaches
   the client.
   RESOLVED 2026-09-25 by
   `033_move_playlist_track_stop_returning_sqlerrm.sql` (#148): the two
   branches that returned `SQLERRM` now return `internal_error` and
   write the detail to the server log with `RAISE LOG`. The finding's
   premise ("never reaches the client") was still true for the HTTP
   client — the leak was in the RPC's own envelope, for a caller that
   invokes it directly. After `033`, query (c) of its README entry shows
   no function in `public` returns `SQLERRM` in the `error` field
   anymore.
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
   RESOLVED by `021_pin_search_path_security_definer.sql` (#120):
   `get_active_users_in_period` and `get_users_with_weekly_stats` now
   carry `SET search_path TO 'public', 'pg_temp'`, and the five
   functions named in (a) — `handle_new_user`, `is_admin`,
   `is_developer_or_higher`, `is_tester_or_higher`,
   `prevent_role_self_update` — move from the bare `'public'` to
   `'public', 'pg_temp'` too. Combined with `move_playlist_track`
   (018), all eight `SECURITY DEFINER` functions in `public` now pin
   `pg_temp` last. This does not touch the `GRANT ... TO anon` left in
   place on the five functions in (a) — that is still open, unchanged
   by this migration.
   RESOLVED 2026-09-22 by
   `025_revoke_anon_execute_scope_helper_policies.sql` (#129): the five
   functions of (a) no longer grant `EXECUTE` to `PUBLIC` or to `anon`.
   Combined with the three `017` already closed, none of the eight
   `SECURITY DEFINER` functions in `public` is executable by `anon`. This
   also corrects a premise the issue relied on — not (a) itself, which
   only says the three `is_*` helpers "return false" for `anon` and
   remains true, but a premise the issue added on top of it: that a
   policy runs with the privileges of the role it belongs to. That one
   does not hold: a policy expression runs with the privileges of the
   role running the query, so revoking `anon` without more would have
   turned the empty result `anon` gets today on `profiles`/`bug_reports`
   into `permission denied`. `025` scopes the
   six policies that call these helpers to `authenticated` for that
   reason, so `anon` no longer evaluates them and keeps seeing the same
   empty result.
   RESOLVED by `032_restrict_aggregation_helpers_to_service_role.sql`
   (#146): `get_active_users_in_period` and `get_users_with_weekly_stats`
   no longer grant `EXECUTE` to `authenticated`; `service_role` is the
   only role that can run them. The finding's opening paragraph
   describes them as granting only to `authenticated` and `service_role`
   after the 2026-09-04 revoke — that was the state until `032`. Both
   return user ids across a whole period without checking the caller,
   and nothing in the backend, in any other function or in the one cron
   job calls them. Of the nine `SECURITY DEFINER` functions in `public`
   (eight in `017` plus `cleanup_library_on_playlist_delete` since
   `031`), these two are the ones closed to `authenticated` too.

Note: comments INSIDE function bodies are verbatim from the database (some
are in Spanish) — they are part of the exported source and are not edited
here. See TODO below.

## TODO

- [x] Translate Spanish comments inside function bodies to English (#56).
      Requires CREATE OR REPLACE in Supabase + updating these files in the
      same change, so repo and DB never diverge.
      RESOLVED by `022_translate_function_body_comments.sql` (#56): a new
      file, not an edit to these files, so 001-021 stay verbatim.