## GET /likes

Lists the authenticated user's active likes, ordered oldest-liked first.

| Case | Status | Body |
|---|---|---|
| Active likes exist | 200 | `ok: true`, `data.likes` |
| No active likes | 200 | `ok: false`, `reason: "no_likes"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty likes list is not an error. See `conventions.md`. A soft-deleted
(unliked) row is excluded here; use `GET /likes/sync` to see it.

Each like has `track_id`, `title`, `artists`, `album`, `album_id`,
`thumbnail_url`, `duration_seconds`, `created_at`, `updated_at` and
`deleted_at`. `artists` is a non-empty list of objects with `id` and
`name`. `duration_seconds` and `deleted_at` can be null; no other field
can be null. `deleted_at` is always null in this endpoint's response.

## POST /likes

Likes a track. Idempotent and revives a previous unlike: upsert on
`(user_id, track_id)`, so liking a track the user already unliked clears
`deleted_at` and restores the row instead of erroring.

| Case | Status | Body |
|---|---|---|
| Track liked (new or revived) | 200 | `ok: true`, `data` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `track_id`, `title`, `artists` (non-empty list),
`album`, `album_id`, `thumbnail_url`. Optional: `duration_seconds`,
stored as given or null if omitted — liking a track does not enrich its
metadata from the track catalog. The like is always scoped to the
caller's user id from the auth token; any `user_id` sent in the body is
ignored.

## DELETE /likes/{track_id}

Unlikes a track: sets `deleted_at` on the matching row. Idempotent —
unliking a track that isn't liked (no row, or already unliked) is still
a 200, not an error.

| Case | Status | Body |
|---|---|---|
| Unliked (or already unliked) | 200 | `ok: true` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Scoped to the caller's user id, so a user can only unlike their own
likes.

## GET /likes/sync

Returns everything that changed for the authenticated user since
`since`, active and soft-deleted alike, ordered by `updated_at`, so a
client can keep a local mirror (e.g. SQLite) up to date: apply active
rows as upserts, remove rows where `deleted_at` is set.

| Case | Status | Body |
|---|---|---|
| Changes since `since` | 200 | `ok: true`, `data.likes` |
| No changes since `since` | 200 | `ok: true`, `data.likes: []` |
| Missing or malformed `since` | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

No changes since `since` is `ok: true` with an empty list, not
`no_likes` — `no_likes` only describes an empty `GET /likes`, not an
empty sync window. `since` is an ISO-8601 timestamp and is exclusive:
rows are returned only where `updated_at` is strictly after it, so
passing the previous response's latest `updated_at` as the next `since`
does not re-return that row.
