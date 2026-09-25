# docs/adr

Architecture decision records for Beatly. A record explains why a
non-obvious piece of the design is the way it is: the alternatives
considered and why they were rejected. Before this directory
existed, that reasoning lived in pull request descriptions, which
is where nobody looks for it six months later, once the PR that
carried it has scrolled off the first page.

## File name

    NNN-title-in-kebab-case.md

Three-digit number, zero-padded, followed by a hyphen and a
kebab-case title. The next record takes the next free number.

## Sections

Every record has the same three sections, in this order:

    ## Context
    ## Decision
    ## Consequences

## Immutability

A record, once written, is never edited. It is a record of a
decision made at a point in time, not a living document.

## Supersession

A decision that changes is documented as a new record, which
states explicitly which earlier number it supersedes. The earlier
record is left as it was written.

## Corrections

A record is never edited, so a statement in one that has since
become wrong is corrected here, in this README, not in the record
itself. That covers both sources of obsolescence: a later record
that replaced the decision, and the code moving on from what the
record described.

- `007-playlist-tracks-order-key-design.md`, "Consequences": the
  last bullet says correcting the stale "ACTIVE" note about
  `trg_playlist_tracks_reorder` in `db/migrations/README.md` "is
  left to a future record or fix — out of scope for #133". #135
  made that fix (`db/migrations/README.md`, finding 3's
  `CORRECTED 2026-09-23` addition). The "Decision" section's
  paragraph on the covering index says stage 2 is "the issue that
  makes `add_playlist_track`, `remove_playlist_track` and
  `move_playlist_track` ... maintain `order_key` themselves — the
  same issue that starts reading it as a cursor". #135 is that
  stage 2, and it does neither of those two things for
  `remove_playlist_track` (a delete does not need a key) nor for
  reading `order_key` as a pagination cursor (out of scope, see
  `008-order-key-write-path.md`, "Context"): #135 only makes
  `add_playlist_track` and `move_playlist_track` write the key,
  and `add_playlist_tracks_bulk` write it for every inserted row.
  `008-order-key-write-path.md` supersedes this part of 007's
  scoping of stage 2.
- `007-playlist-tracks-order-key-design.md`, "Decision" and
  "Consequences": `db/backfills/playlist_tracks_order_key.sql`,
  `scripts/generate_playlist_tracks_order_key_backfill.py` and the
  sections of `db/backfills/README.md` this record cites no longer
  exist since `029` (#137), in git history at `2bec7e9`. The
  decision this record makes — that a regenerable backfill lives in
  `db/backfills/`, not `db/migrations/` — still stands.
- `008-order-key-write-path.md`, "Consequences": the first bullet
  says `position` "is still returned in the response body and
  still assigned by the same logic as before" — since `029` (#137)
  its second half no longer holds: `position` is still returned,
  but as a computed index, not assigned by that logic (see
  `009-drop-playlist-tracks-position.md`). The last bullet, "Stage
  3 ... is still pending", is resolved by `029`/that same record.
  The bolded paragraph under "Decision" that fixes the order
  between applying `028` and changing the code applies only to
  `028`; for `029` it is enough that the PR for #137 is merged
  before the file is applied (`009-drop-playlist-tracks-position.md`).
  That same paragraph's accepted sequence names re-running the
  backfill before `028` is applied — that backfill no longer exists
  since `029` (#137), in git history at `2bec7e9`.
- `009-drop-playlist-tracks-position.md`, "Decision" and
  "Consequences": since #139, four things this record says no
  longer hold.
  (a) `position` is no longer "the 1-based index of the returned
  list" — it is the 1-based index across the **whole playlist**, in
  `order_key` order, whether or not that whole playlist made it into
  one response. The repo owner's reason for keeping it global rather
  than resetting per page: the user sees a numbered list ("the 47th
  of 300"), and a client walking a paginated endpoint by cursor alone
  cannot compute that number itself.
  (b) `GET /playlists/{id}` no longer returns tracks at all, so it no
  longer computes `position` with `enumerate(ordered_ids, start=1)`
  over a list it read whole. Tracks move to the paginated
  `GET /playlists/{id}/tracks`, which computes `position` as a count
  of the rows at or before the page's cursor (`preceding`, `0` on the
  first page) plus the row's 1-based index within the page —
  `preceding + index`, not `enumerate` over anything. `GET
  /public/playlists/{id}` is unchanged and still uses `enumerate`
  over the whole list it reads, and reports the same number
  `GET /playlists/{id}/tracks` would for the same track, because that
  list always starts at the first track (`preceding` would be `0`
  there too).
  (c) "still consecutive within the returned order" (Consequences)
  now reads "consecutive across pages when nothing is written to the
  playlist between them" — pagination introduces a page boundary this
  record's single-response world did not have, and a page's own
  `position` values are not read in the same transaction as the count
  they are offset by, so a concurrent write landing between the two
  can shift them by one; accepted, not fixed by an RPC.
  (d) The preceding-rows count this adds is one query per cursored
  page of `GET /playlists/{id}/tracks`. `GET /playlists/{id}` still
  pays a `count="exact"` on every request, but a smaller one: before
  #139 that count came attached to a read of up to 1000 track rows
  (`_list_playlist_tracks`); since #139 it is a count-only query
  (`_count_playlist_tracks`, `.limit(1)`) that reads no tracks. Its
  real cost against production data was not measured before
  implementing (the repo owner's call: measure after shipping, in
  `db/migrations/README.md` or a follow-up issue, not before); if it
  turns out expensive, that is a separate issue, not a reason to
  revert this one.
  `009` is not edited; `position`'s type, its default of `1`, and
  everything else the record's "Files" entry points to are unchanged.
- `008-order-key-write-path.md`, "Context" and "Consequences": since
  #139, `order_key` is no longer the sort key of `GET /playlists/{id}`,
  which returns no tracks at all any more; it is the sort key of the
  paginated `GET /playlists/{id}/tracks`, with the `playlist_tracks`
  row's id as tiebreaker. `GET /public/playlists/{id}` still orders by
  it, unchanged. The "Context" paragraph calling `core/pagination.py`'s
  cursor support over `order_key` "a separate, unstarted change", and
  the "Consequences" bullet saying reading `order_key` as a
  `core/pagination.py` cursor remains out of scope because "nothing
  paginates playlist tracks today", no longer hold: #139 is that change,
  and `GET /playlists/{id}/tracks` reads `order_key` as its keyset
  cursor. The same bullet's other half — `remove_playlist_track` does
  not write or maintain a key — still stands.

## Files

- `001-provider-errors-import-direction.md` — why
  `core/upstream.py` imports `PROVIDER_ERRORS` from the provider
  module instead of the other way around.
- `002-nonexistent-album-id-maps-to-upstream-error.md` — why a
  well-formed but nonexistent `album_id` on `GET /album/{album_id}`
  responds 502, not 404.
- `003-nonexistent-track-id-maps-to-track-not-found.md` — why a
  nonexistent `track_id` on `/tracks/*` responds 404, unlike `/album`
  and `/artist`.
- `004-missing-credits-dialog-is-an-expected-empty.md` — why a
  navigation failure on `/tracks/{track_id}/credits` becomes a 200
  with empty credits when the ADR 003 probe confirms the track
  exists.
- `005-provider-cache-lives-in-the-services.md` — why the Redis cache
  for the external provider's responses is wired into each service
  function instead of `core/search_provider.py`, why every 200
  (including the documented empty ones) is cached and no domain
  exception ever is, and where the eight cached operations come from.
- `006-redis-failure-costs-two-timeouts-per-request.md` — what a hung
  Redis costs per request, measured, and why no circuit breaker is
  added.
- `007-playlist-tracks-order-key-design.md` — why `playlist_tracks` gets
  a fractional-indexing `order_key` instead of a cursor built on
  `position`, why it is base62 text under `COLLATE "C"`, why its index
  is not unique yet, and why its backfill lives outside
  `db/migrations/` and re-applies instead of running once.
- `008-order-key-write-path.md` — why `order_key` is computed in Python
  outside the RPC's lock instead of in plpgsql, why each RPC
  re-validates it under the lock with the same `order_key_conflict`
  error a real index violation would raise, why a collision gets 3
  attempts with no backoff before a 409, why the three RPCs are
  `DROP` + `CREATE` with `move_playlist_track`'s `REVOKE`s repeated,
  and why the deploy is coupled to `028` in a fixed order.
- `009-drop-playlist-tracks-position.md` — why `position`,
  `trg_playlist_tracks_reorder` and `playlist_tracks_reorder()` are
  dropped together rather than one at a time, why `move_playlist_track`
  moves from a two-`UPDATE` renumber over the whole playlist to a
  single-row `UPDATE`, why `position` keeps being returned in the API but
  as an index computed on read instead of a stored value, why the three
  writers use `CREATE OR REPLACE` this time, and why applying this file
  only has to come after the PR for #137 is merged, with no fixed order
  beyond that.
- `010-user-scoped-database-client.md` — why the six user-data domains
  switch from the service-role Supabase client to one authenticated as
  the caller's own JWT, why the playlist catalog upsert stays on the
  service-role client, why the per-user client is a hand-rolled LRU
  cache keyed by JWT rather than built per request or via a new
  dependency, the cache size and the known risk of closing a client
  still in use, and why `cleanup_library_on_playlist_delete()` becomes
  `SECURITY DEFINER` in `031` to keep its pre-`#143` reach.