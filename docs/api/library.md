## GET /library

Lists the authenticated user's library items, cursor-paginated. See the
Pagination section of `conventions.md` for the shared `limit`/`cursor`
query params and the `data.items`/`data.page` shape.

| Case | Status | Body |
|---|---|---|
| Items exist | 200 | `ok: true`, `data.items`, `data.page` |
| Library is empty | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid `sort` or `order` value | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid, expired, or another `sort`/`order` combination's `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"items": [...]}` and an empty
result used to be `ok: false, reason: "no_library_items"`. Both are gone:
an empty result is now a normal empty first page (`ok: true`), per the
Pagination section of `conventions.md`. `no_library_items` is deprecated
and no longer returned by this endpoint.

Query params: `sort` (`added_at` default | `title`), `order` (`desc`
default | `asc`), unchanged from before pagination. No params returns
items newest-added first. Any other
`sort`/`order` value is rejected before the database is queried. The
`cursor` belongs to the `sort`/`order` combination it was emitted under —
if the client changes the order, it discards the cursor and requests the
first page; reusing a cursor from a different combination is 422
`invalid_cursor`, not a silently mis-ordered page.

Each item has `kind`, `external_id`, `title`, `thumbnail_url`,
`artist`, `artist_id`, `album_id`, `album_name`, `source`, `added_at`
and `updated_at`. `thumbnail_url`, `artist`, `artist_id`, `album_id`
and `album_name` can be null. No internal row id is exposed — the only
identity fields are `kind` and `external_id`, per Track identity in
`conventions.md`.

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
