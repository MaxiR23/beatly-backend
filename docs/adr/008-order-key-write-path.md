# 008. order_key is computed in Python, outside the RPC's lock, and validated under it

Why `add_playlist_track`, `add_playlist_tracks_bulk` and
`move_playlist_track` receive an already-computed `order_key` instead of
generating it themselves, why each one re-validates that key under its
own lock instead of trusting the caller, why a collision gets exactly 3
attempts with no backoff before a 409, why the three RPCs are `DROP` +
`CREATE` instead of an overload, and why the trigger and `position` are
untouched. Continuation of ADR 007 ("stage 2" there); partially
supersedes it (see `docs/adr/README.md`, Corrections).

## Context

ADR 007 added `order_key` as a nullable column with a non-unique index
and a one-time backfill, and deliberately stopped there: nothing wrote
or read the column yet. `#135` is the next step: `order_key` becomes
`NOT NULL`, its index becomes unique
(`db/migrations/028_use_playlist_tracks_order_key.sql`), and the three
RPCs that add or move a row start writing it, while `GET
/playlists/{id}` and `GET /public/playlists/{id}` start reading it
instead of `position`.

Fractional indexing needs the value that sits between two real
neighbours at the moment of the write. The three RPCs already take a
per-playlist row lock before touching `playlist_tracks`
(`013`'s write protocol: `PERFORM 1 FROM playlists WHERE id = ...
FOR UPDATE`), so the neighbours a writer sees while holding that lock
cannot change under it. That makes plpgsql, not the caller, the place
that would naturally look up the immediate neighbours and hand the
generated key straight to `generate_key_between`.

`remove_playlist_track` is out of scope: a delete does not create a new
key, so it has nothing to write. Reading `order_key` as a keyset cursor
for `core/pagination.py` is also out of scope: this issue only changes
what the two `GET` endpoints pass to `.order()`, not how they paginate,
and `core/pagination.py`'s cursor support is a separate, unstarted
change. Dropping `trg_playlist_tracks_reorder` and `position` itself is
stage 3, together, because the trigger's only remaining job (the one
`005`/`009` describe, and that ADR 007 already found disabled live) is
keeping `position` dense, and neither `position` nor the trigger has any
reader left once stage 3 removes the last thing that keys off `position`
order.

## Decision

**The key is generated in Python (`fractional_indexing`), from a read
taken before the RPC call, not inside the RPC.** The alternative —
reading the immediate neighbours in plpgsql, under the lock, and calling
the same spacing algorithm from a `DO` block or a second language runtime
inside Postgres — was rejected for the reason ADR 007 already gives for
not reimplementing the backfill's key generation in plpgsql: the pinned
`fractional_indexing` library is the audited, off-the-shelf
implementation of a closed spec, and reproducing its spacing rule a
second time, in a second language, inside the database is exactly the
kind of bespoke reimplementation ADR 007 already avoided once. Computing
in Python also means `move_playlist_track`'s neighbour read (a `.range()`
window of at most 3 rows around the destination) and `add`/`bulk`'s
neighbour read (the current last key) are ordinary `select` calls the
service already knows how to make and test, with no new SQL to write
for the read side.

**Reading, computing and writing is not atomic, so the RPC re-validates
under its own lock instead of trusting the caller.** The read happens
before the RPC takes the lock, so another writer can change the
neighbours between the read and the write. Rather than accept that
window silently, each RPC checks, right before its success `RETURN`,
that the key it received still falls strictly between the row's real
neighbours (by `id`, not by matching the key's own text — see
`db/migrations/028_use_playlist_tracks_order_key.sql`'s header and
`db/migrations/README.md`'s `028` entry for the exact guard per
function, and the fix that made `add_playlist_tracks_bulk`'s guard
identify its own inserted rows by the `id` its `INSERT ... RETURNING`
returns, not by matching `order_key` values a concurrent writer could
have computed identically). A guard that fails raises an exception
tagged with the same `SQLSTATE`/`CONSTRAINT` a real violation of the new
unique index (`ux_playlist_order_key`) would carry, so it is caught by
the same `EXCEPTION` branch and answered with the same `{"ok": false,
"error": "order_key_conflict"}` — one behavior for "the key already
exists" and "the key is merely out of order", because from the caller's
side both mean the same thing: recompute and retry. This is the same
reasoning finding 7 already applied to `move_playlist_track`'s
privileges: don't rely on a caller behaving, when the database can check
for itself under the lock it already holds.

**3 attempts, no backoff, then a 409 `order_key_conflict`; nothing is
retried beyond that.** The playlist lock already serializes every writer
touching the same playlist, so a retry is only competing with another
write that landed in the same narrow window between someone else's read
and their own write — not with an unbounded number of concurrent
writers. Backoff exists to spread out contention under real concurrency;
here the contention is bounded by how many writers can race for the same
playlist lock in practice, which is small, so a fixed, immediate retry
converges fast without adding latency to the common, uncontended case.
3 was chosen as enough attempts to absorb one or two colliding writers
without making a legitimately stuck client wait through a long retry
loop. Giving up returns a 409, not a 500 or a silent partial write: the
request is safe to retry from the client, and every RPC's `EXCEPTION`
clause wraps its body in an implicit savepoint, so a guard that fires
rolls back everything that RPC wrote before it — the response never
reports a conflict while a partial write survives underneath it.

**The three functions are `DROP` + `CREATE`, not `CREATE OR REPLACE`,
and `move_playlist_track` repeats its `REVOKE`s.** Adding a parameter
changes the argument list, and `CREATE OR REPLACE FUNCTION` cannot
change a function's argument list — it would create a second, overloaded
function instead of replacing the first, leaving the 3-argument version
callable (and, after this migration's `SET NOT NULL`, callable but
broken) alongside the new one. `DROP` + `CREATE` resets privileges to
Postgres's defaults, which for `add_playlist_track` and
`add_playlist_tracks_bulk` happen to match what is already granted
(`017`'s default privileges hand `anon` back its grant, matching the
status quo), but not for `move_playlist_track`: it is `SECURITY DEFINER`
with `REVOKE ALL ... FROM PUBLIC` and no grant to `anon`
(finding 7). Skipping the repeated `REVOKE` after this migration's
`CREATE` would silently reopen the hole finding 7 closed once, for a
`SECURITY DEFINER` function — the migration repeats it explicitly rather
than relying on nobody noticing the gap.

**Deployment is coupled, in a fixed order, not backward compatible.**
The new RPC signatures do not exist until `028` is applied, and the code
deployed alongside it calls those signatures unconditionally — there is
no version negotiation, no dual-write period, and no fallback to the
old 3-argument signatures once `028` drops them. The accepted sequence
is: stop writes to playlists (maintenance mode, or the lowest-traffic
window), re-run the backfill and confirm its "After running" checks are
all zero, apply `028` and confirm its own checks, deploy the code, reopen
writes. A partial rollback (reverting only the code, or only `028`)
leaves `add_playlist_track`, `add_playlist_tracks_bulk` and
`move_playlist_track` answering 502 for the mismatched half, which is an
accepted, temporary state during the deploy window, not a supported
steady state. This was accepted because no client calls these three RPCs
directly against Supabase outside this backend (confirmed with the repo
owner before implementation) — the only caller of the old and new
signatures is this service, deployed as one unit.

**The trigger and `position` are untouched, deferred to stage 3
together.** `trg_playlist_tracks_reorder` is disabled live (ADR 007) and
nothing in this change re-enables it; `position` keeps being assigned
and renumbered by the same plpgsql it always was, in all three RPCs.
Dropping either now, without the other, would leave a component with no
remaining reader (the trigger keeps `position` dense; `position` itself
stops mattering once nothing reads it for ordering) still sitting in the
schema. Retiring both together, once nothing reads `position` for order
any more, is cleaner than retiring them one at a time across two
separate migrations.

## Consequences

- `order_key` is now the sort key of `GET /playlists/{id}` and `GET
  /public/playlists/{id}`; `position` is still returned in the response
  body and still assigned by the same logic as before (`MAX(position) +
  1` for add and bulk, the negative-and-back swap for move) — no
  contract shape changed, only which column is behind `ORDER BY`.
- A write that keeps losing the race gives up after 3 attempts and
  returns 409 `order_key_conflict`; nothing about the request is
  retried automatically beyond that inside the server. The client is
  expected to retry the whole request if it still wants the write to
  happen.
- Reading `order_key` as a `core/pagination.py` cursor, and
  `remove_playlist_track` writing or maintaining a key, both remain out
  of scope. Neither is needed for this stage: a delete has no key to
  write, and nothing paginates playlist tracks today.
- Between apply and deploy, `add_playlist_track`,
  `add_playlist_tracks_bulk` and `move_playlist_track` are expected to
  answer 502 for whichever side (old code against the new signatures, or
  new code against the old ones) is momentarily mismatched — this is the
  accepted cost of the coupled deploy, not a bug to guard against in
  either the migration or the service.
- Stage 3 (dropping `trg_playlist_tracks_reorder`, `position`,
  `ux_playlist_pos` and `idx_playlisttracks_playlist_pos`) is still
  pending, and this record does not schedule it.
