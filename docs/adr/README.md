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
