## POST /plays

Logs a play event for the authenticated user. Append-only: every call
inserts a row, there is no upsert and no deduplication.

| Case | Status | Body |
|---|---|---|
| Play logged | 200 | `ok: true`, `data` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `track_id`, `title`, `artists` (non-empty list), `album`,
`album_id`, `thumbnail_url`. Optional: `duration_seconds`. The body is the
same track shape `POST /likes` takes, and every field except `track_id` is
stored together in the row's `metadata` object — logging a play does not
enrich the track from the catalog and writes nothing to `tracks`.

The response has `track_id`, `metadata` and `played_at`. `played_at` is
set by the server; a `played_at` sent in the body is ignored, as is a
`user_id` — the play is always scoped to the caller's user id from the
auth token.

## GET /plays

Deferred. Reading play history lands with the stats issue, where it needs
date filtering and aggregation rather than a plain list. The route is not
registered yet, so it currently answers 404.

## POST /recents

Registers an entity the user just opened or played. Upsert on
`(user_id, entity_type, entity_id)`: re-registering the same entity
updates the existing row and moves it to the top by refreshing
`played_at`, so it never creates a duplicate.

| Case | Status | Body |
|---|---|---|
| Entity registered (new or moved to top) | 200 | `ok: true`, `data` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `entity_type`, one of `album`, `artist` or `playlist`,
and `entity_id`. Optional: `metadata`, a free-form object stored as-is
(`title`, `thumbnail_url`, `subtitle`…) so a client can render the recents
shelf without a second lookup; it defaults to `{}`. Tracks are not a
recent entity — a played track goes to `POST /plays`.

`played_at` is set by the server on every write, so it is what moves the
row to the top. Any `played_at` or `user_id` in the body is ignored.

## GET /recents

Lists the 30 entities most recently registered by the authenticated user,
newest first, in a single page. Uses the paginated response shape
(`data.items` + `data.page`) and the `limit`/`cursor` query params from the
Pagination section of `conventions.md`, with two particularities explained
below: the response never has a next page and any `cursor` sent is
rejected.

| Case | Status | Body |
|---|---|---|
| Recent entities exist | 200 | `ok: true`, `data.items`, `data.page` |
| No recent activity | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Any non-empty `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"items": [...]}` and an empty
result used to be `ok: false, reason: "no_recents"`. Both are gone: an
empty result is now a normal empty first page (`ok: true`), per the
Pagination section of `conventions.md`. `no_recents` is deprecated and no
longer returned by this endpoint.

There is no `sort` or `order`: the order is fixed, `played_at` descending.

The 30 is a read limit, not a retention policy: the table keeps every row
the user has ever registered and this endpoint never trims or deletes.
Trimming old recents is deferred to its own issue. **The cap is applied as
a bound on `limit`**: the response carries `min(limit, 30)` items in a
single page, `has_more` is always `false` and `next_cursor` is always
`null`. Asking for a `limit` above 30 is not an error: it is capped, and
`data.page.limit` reports the effective limit (asking for 50 returns
`limit: 30`). A `limit` below 30 returns exactly that many and no more:
**there is no next page to request** — to see the 30, the client asks for
`limit` 30 or more. `total` is capped at the same bound and is exact
relative to what this endpoint exposes: a user with 8000 rows sees
`total: 30` and one with 7 sees `total: 7`; with `limit=10` and 8000 rows
the page carries 10 items and `total: 30`.

This endpoint **never emits a `cursor`**, so any `cursor` received is one
that could not have come from it, and is answered 422 `invalid_cursor`
without reaching the database. The param exists because the shared
pagination dependency declares it. The one exception is an empty
`cursor` (`?cursor=`): the shared dependency reads it as the absent
cursor, not as a malformed one, so it is answered 200 like any other
first page. That is the same reading every paginated endpoint gives it.

Each item has `entity_type`, `entity_id`, `metadata` and `played_at`. No
internal row id is exposed — the identity of an item is its `entity_type`
and `entity_id`.

**The result is a snapshot of the moment, not a stable one.** `played_at`
is mutable: a `POST /recents` of an already-registered entity moves its
row to the top (see above), and the top-30 cap is recomputed on every
read. Two successive calls can then return different sets: whatever the
user just played moves to the top and pushes out whatever was entity
number 30. This is the expected behavior of a "recents" list, not
corruption.
