# 012. GET /search validates each row on its own

Why the three mappers of `services/search_service.py` skip and log a row
that misses a required field or carries a wrong-typed one instead of
failing the whole search, why a list the provider answered with rows and
none survived is a 502, why `playlist_id` left the album item, and which
alternatives were rejected (#182).

## Context

A single provider row with a missing field used to take down the whole
response: a song with no album raised a `KeyError` or a
`ValidationError`, which `translate_upstream_errors()` turned into 502
`upstream_error` for a search that had 49 good rows. The issue asked to
fix one key, `playlistId` on albums. The repo owner widened it to the
class: one bad row must never fail `GET /search`.

`translate_upstream_errors()` gives `pydantic.ValidationError` one
meaning: the provider's response does not have the expected shape, 502
(ADR 005 relies on it, and keeps the cache read outside the block for
that reason). Skipping a row gives `ValidationError` a second meaning
inside the same block, for `/search` only.

## Decision

**Per-row validation in the three mappers.** Each row is mapped on its
own. A required field of the contract that is absent or `null`, a row
that is not an object, a malformed `thumbnails`, and a field whose value
pydantic rejects (`title: 5`, `duration_seconds: "3:20"`, ...) skip that
row and log one `WARNING` with the kind and the field names (never `q`,
never the row contents, never the exception text, which carries the
input). Fields the contract allows to be null (`year`, `thumbnail_url`,
`artists`) are read with `.get`. Missing fields are detected explicitly
before the model is built; there is no `try/except KeyError`. The one
`except` is on `ValidationError` only, per row, with a comment citing
this record. It is the owner's deliberate exception to "no `except` that
swallows an error": the row is logged and the response reflects it (next
paragraph). Any other exception still reaches
`translate_upstream_errors()`. A song without `album` or `album_id` is
skipped, not returned with nulls: both are required in `SearchSong`, and
making them nullable would be a contract change nobody asked for.

**Rows came back and none survived: 502, nothing cached.** Applied per
list, to songs and to albums separately. If the provider returned rows
for one of them and every row was dropped, `_require_survivors` raises
the existing `UpstreamError` (502 `upstream_error`); no new reason and no
new `AppError`. It is raised inside `_fetch_search()`, before `cache_set`,
so nothing is written. A provider answer with no rows at all is a normal
empty result and a cached 200. The artist (`limit=1`) is excluded: a
dropped artist row only makes `data.artist` null. Only the first artist
row is looked at; a first row that is not an object gives a null artist,
it does not fall to the second. A payload that is not a list is a layout
change and stays a 502.

**`playlist_id` is dropped from the album item.** It was the key that
started the issue and no published client reads it (owner's
confirmation). Clients that need the playlist use `GET /album/{id}`, where
it is `audio_playlist_id`. A cached value that still has it validates and
is served without it.

**Alternatives rejected:**

- *Fix only `playlistId`.* Leaves the same failure for every other field
  of every row; the next provider quirk reproduces the issue.
- *`try/except KeyError` around each mapper, dropping the row.* Swallows
  the error and returns an empty value, which `CLAUDE.md` forbids, and
  hides a bug of the mapper itself as missing data.
- *Make `album` and `album_id` nullable on `SearchSong`.* A contract
  change that the owner did not ask for.
- *Tolerate wrong types by letting them reach pydantic as a 502.* The
  first plan did that; the owner reversed it in the plan review so that
  one wrong-typed row cannot fail the search either.
- *A partial threshold (502 if more than half of the rows are lost).*
  Not requested; the owner asked for the case "all of them" only.
- *Strict pydantic mode.* It would change validation for the whole model,
  the cache read included, and nobody asked for it.
- *Sharing `_artist_refs` and `_thumbnail_url` with `album_service.py`.*
  The copies already differ in behavior; extracting them is its own
  change.

## Consequences

- A defect that used to be a visible 502 is now a missing result for the
  client. The only signal is the `WARNING` in the server log. A partial
  defect (19 of 20 rows skipped) is a 200 cached for one hour.
- A format change that breaks every row of one list is a 502 on every
  request. Since a 502 is not cached, each request calls the provider
  again and logs one line per row plus one per list, up to about 40 lines.
  That is the intended signal, and it can be noisy.
- Pydantic's lax coercion still applies: `duration_seconds: "200"` is
  accepted as 200 and `True` as 1.
- The per-row `except` also sees a bug of the mapper itself (a misnamed
  field would make every row fail). It becomes a 502 instead of an empty
  200, and the test that maps a good row field by field catches it before
  merge.
- A nullable field with a wrong type now skips the row: an album with
  `year: 2017` disappears instead of going out with `year: null`.
- The ordering rule of `docs/api/search.md` ("nothing is dropped")
  holds for the ordering only; skipped rows are dropped before it.
- ADR 005's statement that `ValidationError` has a single meaning inside
  the translated block no longer holds for `/search`; see the correction
  in `docs/adr/README.md`.
- Only `/search` validates each row on its own. The other provider
  mappers (`album`, `artist`, `tracks`) have no per-row `ValidationError`
  catch, so a row with a missing or wrong-typed field still fails the
  whole response. The related-content mapper in
  `services/track_service.py` already skips items that are not objects,
  without a log; that shape check predates this record.
