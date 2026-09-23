# 007. playlist_tracks gets a fractional order_key, nullable and unenforced until writers exist

Why `playlist_tracks` gets a fractional-indexing `order_key` instead of
continuing to rely on `position` for a future cursor, why the key is
base62 text under `COLLATE "C"`, why its covering index is not unique
yet, why the backfill that fills it lives outside `db/migrations/` and
re-applies instead of running once, and why `fractional-indexing==0.1.3`
is the library despite dating from 2023.

## Context

`position integer NOT NULL` on `playlist_tracks` is renumbered by
`move_playlist_track` (current body in `022`, lines 216-241) on every
move: the RPC pushes every row of the playlist to a temporary negative
position (`ROW_NUMBER() OVER (ORDER BY position)`, one `UPDATE` with no
`WHERE` narrowing it to the rows between the old and new index) and then
flips every negative value back to positive in a second `UPDATE` over the
same rows — moving one track rewrites every row of the playlist, not just
the ones between its old and new position. `playlist_tracks_reorder()`
(`db/migrations/005_playlists_positions.sql`), the trigger that would
otherwise keep `position` dense on a plain `INSERT`/`UPDATE`/`DELETE`, is
disabled live (`tgenabled = D`, matching `017`'s own dump, confirmed
against the live database on 2026-09-23) — it is not what maintains
`position` today. `remove_playlist_track` (current body in `017`) is a
plain `DELETE` with no renumbering step of its own, so `position` is not
guaranteed dense in the live database: a removal can leave a gap that
persists until the next `move_playlist_track` call on that playlist
compacts the whole thing back to 1..count as a side effect of the
negative-and-back swap above. That reorder cost is orthogonal to the
reason `position` cannot
serve as a keyset cursor (`core/pagination.py`'s header): a cursor is
compared against the last row of the previous page, and a reorder that
runs between two page requests shifts the `position` of rows the cursor
has not seen yet, so the same row can be skipped or repeated depending on
which side of the shift it lands on. `core/pagination.py` also already
treats `position` as capped: `ValueType.INT` is documented there as
"every integer sort key here is a small counter (position caps at
1000)", and `services/playlist_service.py`'s own `_TRACKS_LIMIT = 1000`
is "an explicit cap, not pagination" on reading a playlist's tracks —
there is no real pagination for tracks today, only a fixed truncation,
precisely because `position` is not fit to be a cursor.

Fractional indexing avoids both problems: each key sits strictly between
its neighbors, so inserting or moving one row only ever rewrites that
row's own key, and comparing two keys never depends on any third row.
That is also the property that makes it usable as a keyset cursor later.

The issue (#133) scoped this record's decisions to a first step only:
add the column and a matching index, backfill it from the current
`position`, and change nothing the application reads or writes yet.
Reading `order_key` as a cursor, and keeping it in sync on every insert
and move, is explicitly out of scope, left to a later issue ("stage 2"
below).

## Decision

**Fractional indexing, not a rethink of `position` itself.** The
alternative of building a cursor directly on `position` (a keyset pair of
`(position, id)`, or a windowed cache of positions, with a renumbering
step added on every write to force it dense) was not pursued: it does
not remove the reorder-rewrites-many-rows cost, and it does not remove
the mid-pagination shift, so it would not actually close the two
problems above. Fractional indexing removes both by construction, and
per the issue's own reasoning, the key generation algorithm is "not
something to reimplement in plpgsql" — an off-the-shelf, audited
implementation is preferable to a bespoke spacing scheme.

**`fractional-indexing==0.1.3`, pinned on purpose even though it has not
released since 2023.** Fractional indexing is a closed specification —
generate a string strictly between two neighbors, or before/after a
missing bound — not a moving target; independent ports in other
languages of the same scheme agree on the base62 alphabet and the
spacing rule, so
a library that has not needed a release since 2023 is not evidence of
neglect. Verified directly against the pinned version on Python 3.14: it
installs, `generate_n_keys_between(None, None, n)` produces strictly
increasing ASCII strings for `n` up to 100000 with no duplicates, and the
`i`-th key does not depend on `n` (stable by prefix across every length
boundary checked, including 62/63 and 3843/3844) — the property the
backfill's own determinism relies on.

**Keys are base62 text, and the column and its index use `COLLATE
"C"`.** Base62 keys mix digits, uppercase and lowercase ASCII
(`generate_n_keys_between`'s own alphabet), and Postgres's default
collation does not sort that alphabet bytewise — it applies
locale-aware, case-insensitive-ish ordering that would put keys out of
the order the library generated them in. `COLLATE "C"` forces a strict
byte-by-byte (`memcmp`) comparison, the same order
`str.encode("ascii") < ...` gives in Python, so the database and the
generator agree on ordering without either one adapting to the other.

**The covering index `(playlist_id, order_key, id)` is not unique at
this stage.** `order_key` is nullable, and nothing in the application
writes it yet — that is this issue's own scope. Between backfills, rows
in the window (inserted after the last run, or moved by a reorder since
then) sit with `order_key IS NULL` or with a key that no longer matches
their `position`, and neither state is enforceable by a unique
constraint without also deciding, in this issue, how every writer keeps
the column correct. Uniqueness together with `NOT NULL` is deferred to
stage 2, the issue that makes `add_playlist_track`, `remove_playlist_track`
and `move_playlist_track` (the RPCs of `011`-`014`/`018`/`022`) maintain
`order_key` themselves — the same issue that starts reading it as a
cursor and therefore needs `core/pagination.py`'s hard "sort column must
be NOT NULL" rule to hold.

**The backfill is a script-generated, re-appliable `.sql` file in
`db/backfills/`, not a numbered migration and not a script that talks to
Supabase.** Two alternatives were rejected before this one:

- Generating the fractional keys in plpgsql, inside the schema migration
  itself. Rejected for the same reason the issue gives for not
  reimplementing the algorithm at all: the spacing scheme is the part
  worth reusing from an audited library, not reproducing in a second
  language inside a `DO` block.
- A Python script that connects to Supabase directly (with `core/database.py`'s
  client or a service-role key of its own) and writes `order_key` over
  the wire. Rejected because every other schema change in this repo is
  "applied by hand" as a reviewable `.sql` file (`db/migrations/README.md`),
  and a script with its own database credentials would be the first piece
  of Python in the repo that touches Supabase outside of `app.py`'s
  request path, self-triggered and outside code review of what statements
  it actually runs.

  The chosen shape keeps the reviewable-`.sql` property: the script
  (`scripts/generate_playlist_tracks_order_key_backfill.py`) never
  connects to anything — it only imports the library and the standard
  library — and renders a deterministic `UPDATE ... FROM (VALUES ...)`
  lookup of `(position, order_key)` for `position` 1..N, which the repo
  owner applies by hand exactly like a migration.

- A numbered migration (`db/migrations/028_...`), the first draft of this
  plan. Rejected once it was clear the backfill has to be re-run — the
  last time right before stage 2 — because a reorder between runs leaves
  stale keys that only a fresh run corrects. `db/migrations/README.md`'s
  own rule is that an applied migration is history and is never
  re-applied, only ever superseded by a new numbered file; re-running the
  same numbered file would violate that, and a new number per run is not
  viable either, since the file is meant to be run an unbounded number of
  times. `db/backfills/` holds exactly this second kind of file: SQL that
  is regenerated (if the generator or its `N` changes) and re-applied on
  purpose, documented in its own `db/backfills/README.md` instead of
  `db/migrations/README.md`'s "applied once" convention.

  Each run overwrites every row's key to match its current `position`
  (filtered with `AND pt.order_key IS DISTINCT FROM k.order_key` so a row
  that already carries the right key is not rewritten), not only the rows
  that are `NULL` — a reorder inside the window desynchronizes existing
  keys from `position` without ever setting them back to `NULL`, so only
  overwriting nulls would leave those rows silently wrong.

**The backfill disables and re-enables `trg_bump_playlist_on_track_change`
inside its own transaction.** That trigger (`017` line 2110, `AFTER
INSERT OR DELETE OR UPDATE ... FOR EACH ROW`, no column filter) bumps
`playlists.updated_at`, a value every playlist response exposes. Left
enabled, every backfill run would bump `updated_at` on every playlist
that has tracks, for a change nothing in the application can see yet.
The `.sql` file's `BEGIN; DISABLE TRIGGER ...; UPDATE ...; ENABLE TRIGGER
...; DO $$ ... $$; COMMIT;` order (`db/backfills/playlist_tracks_order_key.sql`)
keeps `updated_at` untouched and re-enables the trigger before `COMMIT`
on every successful run, so the trigger is never left off in the
database's committed state — only a partial, hand-run statement outside
a transaction could leave it disabled, which is why
`db/backfills/README.md` documents a check for that case.

## Consequences

- `order_key` sorts identically to `position` today (enforced by the
  `DO $$ ... $$;` block the backfill runs before `COMMIT`, which aborts
  the whole transaction if any row is left `NULL`, any `(playlist_id,
  order_key)` pair repeats, or the two orders disagree for any playlist),
  but nothing reads it yet. It is inert until stage 2.
- A window between backfill runs is accepted, not closed: a track
  inserted after a run, or a playlist reordered after a run, carries
  either `order_key IS NULL` or a key out of sync with its `position`
  until the next run. `db/backfills/README.md` documents this instead of
  hiding it, and the last run has to land right before stage 2 starts
  reading the column.
- Stage 2 cannot make `order_key` the sort key of `core/pagination.py`'s
  shared helper as-is: that helper hard-requires `NOT NULL` on the sort
  column, and nothing here makes that true. Stage 2 has to both close the
  window (a final backfill run, or a trigger/RPC change that fills the
  key on write) and add the `NOT NULL` and unique constraints this issue
  deliberately does not add.
- Measured live on 2026-09-23: `trg_playlist_tracks_reorder` is disabled
  (`tgenabled = D`, matching `017`'s own dump) while
  `trg_bump_playlist_on_track_change` is enabled (`tgenabled = O`).
  `position` is not guaranteed dense live: `remove_playlist_track` deletes
  without renumbering, so a gap can persist between one removal and the
  next `move_playlist_track` call on that playlist, which does renumber
  the whole playlist densely, but only as a side effect of the
  negative-and-back swap it always runs. This is relevant to stage 2, not
  to this one: whichever RPC ends up keeping `order_key` current on write
  should not assume the reorder trigger is there to fall back on, since
  live it is not, nor that `position` arrives dense between its own
  calls. Correcting the stale "ACTIVE" note about that trigger in
  `db/migrations/README.md` is left to a future record or fix — out of
  scope for #133.
- The `.sql` file this issue produces is large by this repo's standards
  (197148 bytes, ~197 KB, for `N = 10000`, versus `017`'s 98567 bytes),
  because it embeds one `VALUES` row per position; that is an accepted
  cost of keeping the key generation in Python and the application step
  in plain SQL.
