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

Lists the authenticated user's 30 most recently registered entities,
newest first.

| Case | Status | Body |
|---|---|---|
| Recent entities exist | 200 | `ok: true`, `data.items` |
| No recent activity | 200 | `ok: false`, `reason: "no_recents"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty recents list is not an error. See `conventions.md`.

Each item has `entity_type`, `entity_id`, `metadata` and `played_at`.

The 30 is a read limit, not a retention policy: the table keeps every row
the user has ever registered and this endpoint never trims or deletes.
Trimming old recents is deferred to its own issue.
