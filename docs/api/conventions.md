# API conventions

Rules that apply to every endpoint. Per-endpoint documents only
describe what differs from this.

Field-level schemas are in OpenAPI: http://localhost:8000/docs

## Response shape

    {"ok": true, "data": ...}
    {"ok": false, "reason": "..."}

`ok` is always present. `reason` is a stable snake_case identifier
meant to be read by the client, never a message to display.

## Status codes

| Case | Status | reason |
|---|---|---|
| Success | 200 | none |
| Expected empty state | 200 | specific to the case |
| Parent resource does not exist | 404 | `*_not_found` |
| Invalid input | 422 | `invalid_request` |
| Conflict with current state | 409 | specific to the case |
| Not authenticated | 401 | `unauthorized` |
| No permission | 403 | `forbidden` |
| Upstream service failed | 502 | `upstream_error` |
| Upstream timed out | 504 | `upstream_timeout` |
| Unhandled internal error | 500 | `internal_error` |

## The distinction that matters

A 200 with `ok: false` is not an error. The request succeeded and the
answer is that there is nothing. No endpoint returns this shape today:
every list that could is paginated, and a paginated one answers "nothing
here" with an empty first page instead, as the Pagination section
describes.

A 404 means the parent resource does not exist. A slug that matches no
genre returns 404.

Clients check `ok` in the body, not the status code, to tell "nothing
here" from "navigation error".

## Reasons

| reason | Status | Meaning |
|---|---|---|
| `invalid_request` | 422 | Missing or malformed parameters |
| `unauthorized` | 401 | Missing or invalid token |
| `forbidden` | 403 | Authenticated but not allowed |
| `upstream_error` | 502 | A provider or the database failed |
| `upstream_timeout` | 504 | A provider did not respond in time |
| `internal_error` | 500 | Unhandled error. If you see this, it is a bug |
| `profile_not_found` | 404 | Authenticated user has no profile row |
| `library_item_not_found` | 404 | No library item matches user_id, kind, external_id |
| `username_taken` | 409 | Requested username already belongs to another profile |
| `report_not_found` | 404 | No bug report matches the given id |
| `playlist_not_found` | 404 | No playlist matches the given id. On `/playlists/{id}` it also covers a playlist the caller cannot edit, which is deliberately indistinguishable from one that does not exist. On the `/public/...` endpoints it also covers a playlist that exists but is not public, deliberately indistinguishable from one that does not exist. Also returned by `GET /genre-playlists/{playlist_id}/tracks`, where the id is that of a curated genre playlist |
| `track_already_in_playlist` | 409 | `POST /playlists/{id}/tracks` was given a track the playlist already contains. The bulk endpoint skips such tracks instead of returning this |
| `order_key_conflict` | 409 | A write that places a track (`POST /playlists/{id}/tracks`, `.../tracks/bulk`, `.../move-track`) kept colliding with concurrent writes to the same playlist and gave up after 3 attempts. Nothing was written; the request can be retried |
| `invalid_cursor` | 422 | The cursor is malformed or no longer valid |
| `track_not_found` | 404 | No track on the external provider matches the given id, as reported by its own playability status |
| `genre_not_found` | 404 | No genre matches the given slug. Returned by `GET /genres/{slug}/playlists` and `GET /genres/{slug}/categories` |

## Track identity

A track has two identities: the internal catalog uuid (`tracks.id`,
Postgres-generated) and the external provider id (`tracks.track_id` —
the id the client plays and references, whatever the upstream provider
is).

**The API speaks the provider id only.** The internal uuid never
crosses an endpoint boundary, in either direction: not in request
params or bodies, not in response payloads. Where a table stores the
internal uuid (`playlist_tracks`), the provider->uuid resolution
happens inside the service or the RPC
(`get_owned_playlists_with_track`, `remove_playlist_track`), invisible
to the client.

Rationale: the client only knows provider ids; leaking the internal
uuid creates two ways to reference the same track and forces every new
domain to re-decide which one to accept. The provider is an
implementation detail: if it ever changes, `tracks.track_id` keeps
being "the external id", and neither the rule nor any endpoint
contract moves.

## Pagination

Every endpoint that returns a list that can grow uses cursor
pagination. Query params: `limit` (default 50, max 100, enforced
server-side) and `cursor` (opaque — the client stores and echoes it,
never inspects or builds it).

Paginated responses wrap the list:

    {"ok": true, "data": {"items": [...], "page": {
        "limit": 50, "next_cursor": "..." , "has_more": true,
        "total": 370}}}

`total` is exact and present only on the first page (request without
cursor); `null` on subsequent pages — the client keeps it. End of the
collection is `has_more: false` with `next_cursor: null`. An invalid
or expired cursor is 422 `invalid_cursor`.

Under the hood this is keyset pagination: each domain declares a sort
key (plus id as tiebreaker) and the shared helper in
`core/pagination.py` does the rest. Offset pagination is not used
anywhere: its cost grows with the offset and rows shift between pages.

Keyset pagination is exact while no write changes a row's sort key
during a walk. Where a write can — a reorder that rewrites the key — a
row moved from ahead of the cursor to behind it is skipped for the rest
of that walk, and one moved from behind the cursor to ahead of it is
returned a second time. This is accepted, not a bug, for any endpoint
whose sort key a write can change; its own docs say which writes those
are. An endpoint whose sort key no write changes does not have this
limitation.

A cursor belongs to the ordering it was emitted under. An endpoint that
accepts more than one ordering rejects a cursor emitted under a
different one with 422 `invalid_cursor`, rather than returning a page
sorted the wrong way — so when the client changes the ordering, it
discards the cursor and requests the first page again.

For a paginated endpoint, the expected-empty state is the first page
coming back empty — `ok: true`, `items: []`, `has_more: false`,
`total: 0` — not an `ok: false` with an empty-state reason as described
in "The distinction that matters". That section covers non-paginated
list endpoints; a paginated one only ever has one shape for "nothing
here", the same one it uses for the end of a longer collection.

## Caching headers

Every response carries a `Cache-Control` header, so a client needs no
cache times of its own: the backend is the only source of them. There are
three categories, by domain and not by HTTP method.

| Endpoints | Header on success |
|---|---|
| Served from the server-side cache: `/search`, `/album/{id}`, `/artist/{id}`, the four `/tracks/{id}/...` endpoints, and `/public/album/{id}`, `/public/artist/{id}`, `/public/tracks/{id}` | `max-age=<N>` |
| User data: `/library`, `/likes`, `/playlists`, `/profile`, `/bug-reports`, `/plays`, `/recents`, for every method | `private, no-cache` |
| Everything else (`/genres`, `/genre-playlists`, `/public/playlists/{id}`, `/public/genre-playlists/{id}`, `/health`) | `no-store` |

`N` is the time the server-side cache entry has left, in whole seconds,
not the operation's full TTL. A client that keeps the response for `N`
seconds never holds it longer than the server would still serve it. When
the response was just fetched from the external provider (a cache miss),
`N` is the full TTL, whether or not the entry could be written. The
header carries neither `private` nor `public`: the endpoints that require
a token are already not stored by shared caches, and on the public share
endpoints an intermediary storing the response is desirable. If the
server cannot determine how much time the entry has left, the response
goes out as `no-store` instead.

Every error response, on any endpoint, carries `no-store`, whatever the
success header of that endpoint is. The body, status and reason of a
response never depend on these headers.

## Image size

Every `thumbnail_url`, and every element of `thumbnail_urls` and
`thumbnails`, of a track, an album, a playlist or an artist in a list is
requested from the provider's CDN at 544 x 544 with smart crop, so a
client never gets a 60 x 60 cover to show in a list.

The rule: the first occurrence of `=w<digits>-h<digits>` (ASCII digits
0-9), with an optional immediate `-p` (counted only as a whole flag,
followed by `-` or by the end of the URL), is replaced by
`=w544-h544-p`. `-p` is the smart crop; it is added if missing and never
duplicated. The rest of the URL (for example `-l90-rj`) is left intact,
with no host filter. Examples: `...=w60-h60-l90-rj` becomes
`...=w544-h544-p-l90-rj`, and `...=w226-h226-p-l90-rj` becomes
`...=w544-h544-p-l90-rj`. A larger URL also goes down to 544.

A URL without that suffix (for example one from `i.ytimg.com`) is
returned as is and is never an error; `null` stays `null`.

The URLs a client stores (likes, the tracks of a playlist, the library)
are rewritten when they are read: the database keeps what was sent, and
the responses of `POST /likes`, `POST /playlists/{playlist_id}/tracks` and
`POST /library` already come back rewritten.

The rewrite happens on read, in Python, and not when writing or in a
migration. Rewriting on write would leave the rows already stored small.
A migration plus a backfill would copy the rule into SQL (as `036`
already does for recents), would have to be applied by hand, and would
skip the shared helper. Reading covers old and new rows and stores
nothing new.

Exceptions: the artist image of `GET /artist/{artist_id}` is 1200 x 1200
(see `artists.md`), and `GET /recents` and `POST /recents` go to
512 x 512 without smart crop (see `activity.md`).

A response cached on the server before this rule may carry the previous
URL until it expires (the TTL of each endpoint is in its own document).
