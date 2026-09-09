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
| `playlist_not_found` | 404 | No playlist matches the given id. On `/playlists/{id}` it also covers a playlist the caller cannot edit, which is deliberately indistinguishable from one that does not exist |
| `track_already_in_playlist` | 409 | `POST /playlists/{id}/tracks` was given a track the playlist already contains. The bulk endpoint skips such tracks instead of returning this |
| `invalid_cursor` | 422 | The cursor is malformed or no longer valid |
| `track_not_found` | 404 | No track on the external provider matches the given id, as reported by its own playability status |

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