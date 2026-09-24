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