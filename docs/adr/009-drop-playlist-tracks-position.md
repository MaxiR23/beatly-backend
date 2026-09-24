# 009. Drop playlist_tracks.position; position becomes a computed index

Why `playlist_tracks.position`, `trg_playlist_tracks_reorder` and
`playlist_tracks_reorder()` are dropped together rather than one at a
time, why `move_playlist_track` moves from rewriting the whole playlist's
positions to a single-row `UPDATE`, why the API keeps returning
`position` even though the column is gone, why the three writers use
`CREATE OR REPLACE FUNCTION` this time instead of `DROP` + `CREATE`, and
why applying `029_drop_playlist_tracks_position.sql` only has to come
after the PR for #137 is merged, with no other ordering requirement.
Continuation of ADR 008 ("stage 3" there); corrects part of its
Consequences (see `docs/adr/README.md`, Corrections).

## Context

ADR 008 (`#135`) made `order_key` `NOT NULL`, gave it a unique index, and
switched `add_playlist_track`, `add_playlist_tracks_bulk` and
`move_playlist_track` to write it, while `GET /playlists/{id}` and
`GET /public/playlists/{id}` switched to reading it. It deliberately left
`position`, `ux_playlist_pos`, `idx_playlisttracks_playlist_pos`,
`trg_playlist_tracks_reorder` and `playlist_tracks_reorder()` untouched,
because at that point `order_key` was not yet proven to reproduce the
same order on its own — stage 3 was scheduled for once nothing read
`position` for ordering any more.

`trg_playlist_tracks_reorder` has been disabled on the live database
since before this domain's order-key work started (ADR 007, finding 3 of
`db/migrations/README.md`). Its only job was keeping `position` dense on
delete; `remove_playlist_track` already deletes without renumbering, so
gaps in `position` have been possible in production for a while, with
nothing to close them. `position`'s last remaining reader was
`move_playlist_track`'s own renumbering logic and the two `GET` handlers'
response shape — the `order_key` migration already took ordering away
from it; only the stored value and its upkeep were left.

## Decision

**`position`, its trigger, its trigger function, its unique constraint
and its index are dropped in the same migration, not across several.**
`trg_playlist_tracks_reorder`'s only purpose was keeping `position`
dense, and `playlist_tracks_reorder()` has no other caller. Once
`position` itself has no reader, neither the trigger nor the function has
one either — retiring them separately would leave a dead component
sitting in the schema for no benefit. `029_drop_playlist_tracks_position.sql`
drops, in this order inside one transaction: the trigger, its function,
the constraint, the index, and only then the column — `ALTER TABLE ...
DROP COLUMN` removes any index or constraint over it automatically, so
the explicit drops have to run first or they fail against an object
already gone. A `DO` block copied from `028`, comparing the order by
`position` against the order by `order_key`, runs immediately before any
of this, inside the same transaction — the same gate `028` already
trusted, repeated here because this is the last point at which the old
order still exists to compare against.

**`move_playlist_track` moves from rewriting the whole playlist's
positions to a single-row `UPDATE`.** Its `028` body still computed a
full negative-and-back renumber over every row in the playlist — a
`CREATE TEMP TABLE`, two conditional `UPDATE`s, and a third one to flip
the signs back — purely to keep `position` in step with a move that
`order_key` already fully describes. With `position` gone there is
nothing left for that machinery to maintain: the function now identifies
the row at `p_old_index` by `ROW_NUMBER() OVER (ORDER BY order_key)` and
writes `order_key` on that one row, the only row this call needs to
touch. The guard that used to compare against `position`-relative
neighbours becomes a count: exactly `p_new_index - 1` other rows must
sort before the received key. This is equivalent to the two-sided
neighbour check it replaces, because the caller already knows the key is
distinct from every existing key — `ux_playlist_order_key` fails the
`UPDATE` itself on an exact collision, landing in the same `EXCEPTION`
branch a guard failure would.

**The API keeps returning `position`, computed on read, not stored.**
The repo owner's decision: "The API SIGUE exponiendo `position` por
track, calculado como índice 1-based de la lista devuelta, ordenada por
`order_key`. ... es un dato útil para el cliente y calcularlo del índice
es trivial; sacarlo rompería a los clientes para ahorrar una línea.
`order_key` es un detalle de implementación y el cliente no tiene por qué
conocerlo." `PlaylistTrack.position` and `PublicPlaylistTrack.position`
keep the same type and the same JSON key; only where the value comes
from changes. `GET /playlists/{id}` and `GET /public/playlists/{id}`
compute it in Python with `enumerate(ordered_ids, start=1)` over the rows
already read in `order_key` order — the same pattern `_list_liked_tracks`
already used for the virtual "liked" playlist, which never had a
`position` column to begin with. `add_playlist_track` computes it with a
`SELECT COUNT(*)` taken under the same playlist lock, immediately after
its insert: the guard already in place since `028` leaves the new row
strictly last by `order_key`, so its 1-based index is exactly the row
count — the same number `GET /playlists/{id}` would compute for that
track right after. `move_playlist_track`'s `order` field, returned on
every call including the noop branch, now derives `position` with
`ROW_NUMBER() OVER (ORDER BY order_key)` in its two subqueries instead of
selecting the dropped column; the outer `json_agg(... ORDER BY
t.position)` is unchanged. None of the three writers reads, writes or
orders by `position` anywhere any more.

**The three writers use `CREATE OR REPLACE FUNCTION`, not `DROP` +
`CREATE` like `028` did.** Unlike `028`, no signature changes here — all
three keep exactly their `028` argument lists. `CREATE OR REPLACE`
therefore keeps every grant and revoke `028` already applied, with no
`GRANT`/`REVOKE` needed in this file. It does not keep every attribute,
though: `CREATE OR REPLACE FUNCTION` resets anything not restated in the
new definition, so `move_playlist_track`'s `SECURITY DEFINER` and `SET
search_path TO 'public', 'pg_temp'` are copied verbatim from `028` —
omitting either would silently drop the function back to `SECURITY
INVOKER` or an unpinned `search_path`, with no error at apply time to
catch it.

**Applying `029` only has to come after the PR for #137 is merged; there
is no other ordering requirement, and no required time of day.** The
backend that ships with this PR reads only `"track_id"` from
`playlist_tracks`, and the `add_playlist_track` it calls already returns
a `position` key against a database still on `028` — the new code works
against the old schema. Code from before the merge reads `"track_id,
position"`, which breaks the moment this file drops the column. So the
PR has to merge, and the backend has to be running that code, before
`029` is applied — after that, `029` can be applied at any point; nothing
about it depends on when. This is unlike `028` (ADR 008), where the code
and the migration had to change together in a fixed sequence with no
backward compatibility in either direction: here only one direction
matters, and it holds for as long as the merged code keeps not reading
`position`.

Between the merge and applying `029`, `POST /playlists/{playlist_id}/tracks`
can return a `position` larger than the index `GET /playlists/{playlist_id}`
gives that same track, in a playlist with gaps in the old `position`
column: the add RPC is still `028`'s, computing `MAX(position) + 1` from
the stored column, while the merged code already reads the computed
index for `GET`. This is a real, temporary inconsistency, not a bug —
closing it is exactly what applying `029` does, and until then it is
documented as a known state rather than hidden. See the `029` entry of
`db/migrations/README.md`.

**The `DO` block that checks position order against `order_key` order is
the gate.** Copied verbatim from `028`, it runs first, inside the
transaction, immediately before the trigger and the column it protects
are dropped: this is the last point at which the old order still exists
to compare `order_key` against. A mismatch aborts before anything is
destroyed, rather than after.

## Consequences

- `position` is no longer a column anywhere on `playlist_tracks`; the
  trigger, its function, its unique constraint and its non-unique
  index that supported it are gone. `move_playlist_track` writes exactly
  one row per call instead of rewriting the whole playlist.
- `PlaylistTrack.position` and `PublicPlaylistTrack.position` are
  unchanged in shape: still a required `int`, still 1-based, still
  consecutive within the returned order. It is not a stable identifier —
  it moves when another track is added, removed or moved, same as it
  always could once the trigger stopped closing gaps, only without the
  gaps now.
- `add_playlist_track`'s response and `move_playlist_track`'s `order`
  field keep their `position`/`'position'` JSON keys, now computed under
  the RPC's own lock instead of read from a stored value.
- `db/backfills/playlist_tracks_order_key.sql`, its generator and its
  test are deleted in the same change (#137): once `position` no longer
  exists, that backfill cannot run — its `UPDATE` reads `position` by
  construction. Its last version is in git history.
- Between the PR merging and `029` being applied, `POST
  /playlists/{playlist_id}/tracks` can report a `position` inconsistent
  with what `GET /playlists/{playlist_id}` reports for the same track, in
  a playlist that has gaps in the old `position` column. This closes when
  `029` is applied; it is not fixed by adding a read in Python.
  `029` can be applied at any point after the merge.
- `029` is irreversible: there is no down migration, and the order that
  `position` used to encode is destroyed with the column. Running the
  backend on a commit from before the PR for #137 merged, against a
  database that already has `029` applied, breaks `GET
  /playlists/{playlist_id}` and `GET /public/playlists/{playlist_id}`
  with a 502.
