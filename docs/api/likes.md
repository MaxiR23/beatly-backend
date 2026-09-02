## GET /likes

Lists the authenticated user's active likes, ordered oldest-liked first,
cursor-paginated. See the Pagination section of `conventions.md` for the
shared `limit`/`cursor` query params and the `data.items`/`data.page`
shape.

| Case | Status | Body |
|---|---|---|
| Active likes exist | 200 | `ok: true`, `data.items`, `data.page` |
| No active likes | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"likes": [...]}` and an empty
result used to be `ok: false, reason: "no_likes"`. Both are gone: an
empty result is now a normal empty first page (`ok: true`), per the
Pagination section of `conventions.md`. `no_likes` is deprecated and no
longer returned by this endpoint.

A `cursor` issued before the pagination cursor gained its sort
discriminator no longer decodes and now responds 422 `invalid_cursor`,
same as any other invalid or expired cursor: the client discards it and
requests the first page again. See `GET /likes/sync` for the same note.

A soft-deleted (unliked) row is excluded here; use `GET /likes/sync` to
see it.

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
rows as upserts, remove rows where `deleted_at` is set. Cursor-paginated,
same `limit`/`cursor` query params and `data.items`/`data.page` shape as
`GET /likes` — see the Pagination section of `conventions.md`.

`since` is required only to start a sweep (a request without `cursor`).
Once a page carries a `cursor`, `cursor` alone fixes the position: `since`
is no longer required, and if both are sent, **the cursor wins and
`since` is ignored**. A request with neither `since` nor `cursor` is
rejected before touching the database.

| Case | Status | Body |
|---|---|---|
| Changes since `since` (first page) | 200 | `ok: true`, `data.items`, `data.page` |
| Next page via `cursor` | 200 | `ok: true`, `data.items`, `data.page` (`since` ignored if also sent) |
| No changes since `since` | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false` |
| Neither `since` nor `cursor` | 422 | `ok: false`, `reason: "invalid_request"` |
| Malformed `since` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"likes": [...]}`; it is now
`{"items": [...], "page": {...}}`. `since` used to be required on every
request; it is now required only when there is no `cursor`.

A `cursor` from before the pagination cursor gained its sort
discriminator no longer decodes and responds 422 `invalid_cursor`. A
sweep in progress loses its cursor and restarts from `since` — see the
note on `GET /likes` for the general case.

`since` is an ISO-8601 timestamp and is exclusive: rows are returned
only where `updated_at` is strictly after it, so passing the previous
response's latest `updated_at` as the next `since` does not re-return
that row. No changes since `since` is `ok: true` with an empty list, not
an error.

**Sweep semantics.** `updated_at` is a mutable column and pagination walks
it in ascending order. A row updated while the client is still paging
through a sweep normally moves forward, past the cursor, so it can
**reappear** on a later page: that is a duplicate, not a problem — the
client re-applies the same upsert it already applied, which is
idempotent — but it is new behavior compared to the unpaginated endpoint
and clients should expect it rather than treat it as corruption.

What a sweep does **not** guarantee is that every changed row is seen
within the sweep in which it changed. `updated_at` is stamped when the
write runs, but the row only becomes visible to other transactions when
its transaction commits; a write stamped before the cursor but committed
after the page that produced that cursor is filtered out for the rest of
the sweep. The window is milliseconds wide and it is not new — `since`
has the same property in the unpaginated endpoint — but a client keeping
a local mirror should not assume it away.

The server makes no promise about this window and applies no overlap of
its own. As a client-side mitigation, start each new sweep from a
`since` **60 seconds** earlier than the newest `updated_at` received in
the previous sweep, rather than from that value itself. This does not
guarantee every write is seen — a longer stall could still miss the
window — but it narrows it, and the resulting repeats are the same
idempotent upserts described above.

The `cursor` belongs to one sweep. When starting a **new** sweep, the
client discards any `cursor` it kept and starts again from `since`.
Reusing an old `cursor` does not skip rows — the cursor wins over
`since`, so it only re-emits rows already seen — but it also will not
pick up the changes a fresh sweep from `since` would.
