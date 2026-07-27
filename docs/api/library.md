## GET /library

Lists the authenticated user's library items.

| Case | Status | Body |
|---|---|---|
| Items exist | 200 | `ok: true`, `data.items` |
| Library is empty | 200 | `ok: false`, `reason: "no_library_items"` |
| Invalid `sort` or `order` value | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty library is not an error. See `conventions.md`.

Query params: `sort` (`added_at` default | `title`), `order` (`desc`
default | `asc`). No params returns items newest-added first, matching
the underlying index. Any other `sort`/`order` value is rejected before
the database is queried.

Each item has `kind`, `external_id`, `title`, `thumbnail_url`,
`artist`, `artist_id`, `album_id`, `album_name`, `source`, `added_at`
and `updated_at`. `thumbnail_url`, `artist`, `artist_id`, `album_id`
and `album_name` can be null.

## POST /library

Adds an item to the authenticated user's library. Idempotent: adding
an item that already exists (same user, `kind`, `external_id`) updates
its stored metadata in place rather than returning a conflict.

| Case | Status | Body |
|---|---|---|
| Item added or updated | 200 | `ok: true`, `data` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `kind` (`album` or `playlist`), `external_id`,
`title`, `source`. Optional: `thumbnail_url`, `artist`, `artist_id`,
`album_id`, `album_name`. `added_at`/`updated_at` are server-managed
and cannot be set by the client. The item is always scoped to the
caller's user id from the auth token — there is no `user_id` field to
set, and any `user_id` sent in the body is ignored.

## DELETE /library/{kind}/{external_id}

Removes an item from the authenticated user's library.

| Case | Status | Body |
|---|---|---|
| Item removed | 200 | `ok: true` |
| Item does not exist | 404 | `ok: false`, `reason: "library_item_not_found"` |
| Invalid `kind` | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`kind` must be `album` or `playlist`; any other value is rejected
before the database is queried. Removal is scoped to the caller's user
id, so a user can only remove items from their own library.
