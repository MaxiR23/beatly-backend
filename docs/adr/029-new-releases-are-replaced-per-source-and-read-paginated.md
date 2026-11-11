# 029. New releases are replaced per source, read paginated, and parsed from the raw page

Where the list of new releases is stored, how an ingest replaces it, how the
app reads it and how the collector reads the page (issue 242). It reuses the
shape of 028 (a collector on the owner's phone, one internal endpoint, one
atomic function).

## Context

The app shows a list of new releases. The previous tables for releases
(`feed_current`, `upcoming_releases`, `release_sync_errors`) have no consumer
in this code base. The external provider has a page of new releases that is a
single grid of album cards in one response, and the list depends on the IP of
the caller, so it is read from the phone for the same reason as in 028. A
second source of releases is likely in the future.

## Decision

**A new table, `new_releases`, and not the old ones.** The old tables have
other columns and other keys and nothing uses them; they are left as they are.
The new one holds what the app shows (album id, title, type, artists with
their ids, cover) and the id of the audio playlist, which is stored and not
exposed yet.

**A `source` column.** The key is `(source, album_id)` and the order is
`(source, position)`. `replace_new_releases` (migration `053`) deletes and
inserts only the rows of its own source, in one transaction and under an
advisory lock, so two ingests are serialized, a failure keeps the previous
list, and the same body twice leaves the same rows (the table has no uuid and
no timestamp). `GET /releases` filters by the same source, so the pair
(`position`, `album_id`) is unique for the keyset cursor. How several sources
are combined is the decision of the issue that adds the second one.

**The read is paginated with the shared helper**, ordered by `position` with
`album_id` as the tie-break, and public like `/genres`. It does not follow the
whole-collection exception of the genre endpoints. The list is replaced as a
whole, so a walk across pages that overlaps a replacement can skip or repeat
an item; the client starts again from the first page.

**The collector parses the raw response of the page.** The parsers of the
library drop, without a trace, the artist names that have no link, and a card
with one of those must be discarded whole (measured: 5 of 109 cards).
So the collector calls the private `_send_request` of the library once, with
the language fixed to `en` and the location left free, and applies its own
rules card by card. The method is private: the version pin in
`requirements.txt` and a test of its signature guard it, and a version bump
has to review it. A page with another shape (several sections, continuations)
is unreadable and aborts the run.

**The run aborts, and nothing is sent, when the page cannot be trusted.** No
cards, no card that passes, more than 500 that pass, and fewer than half of the
cards read passing (`MIN_KEPT_RATIO = 0.5`; exactly half does not abort). In
the two samples measured 95% of the cards passed, so a bigger discard means the
format changed, and the app must keep showing the last good list. The rule
lives only in the collector: the back receives the cards that pass and does not
know it.

## Alternatives rejected

- **Reuse `feed_current` or `upcoming_releases`.** Their columns and keys do
  not fit, and nothing would be gained from their data.
- **A table without `source`.** The second source would need a migration of
  the key and of the function while the app is reading.
- **Return the whole collection with no cursor**, like the genre endpoints.
  That exception is documented for lists of tens of items; this list can be a
  few hundred and the shared helper costs nothing.
- **The parsers of the library.** They hide exactly the cards the collector has
  to discard.
- **Abort only when no card passes.** A page whose format changed would replace
  a good list with a few cards.

## Consequences

- If the phone is off or the collector aborts, the app keeps serving the last
  saved list.
- Applying `053` has to come before deploying the code; without it the two
  endpoints answer 502.
- The literal of the source is written twice (the function and
  `services/release_service.py`); an integration test writes with the function
  and reads with the route, so it fails if they diverge.
- A version bump of the library has to check `_send_request` and the shape of
  the page again.
