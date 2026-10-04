## GET /likes

Lists the authenticated user's active likes, ordered oldest-liked first,
cursor-paginated. See the Pagination section of `conventions.md` for the
shared `limit`/`cursor` query params and the `data.items`/`data.page`
shape. `data` also carries `checkpoint`, see
[Checkpoint](#checkpoint).

| Case | Status | Body |
|---|---|---|
| Active likes exist | 200 | `ok: true`, `data.items`, `data.page`, `data.checkpoint` |
| Next page via `cursor` | 200 | `ok: true`, `data.items`, `data.page`, `data.checkpoint` (the first page's) |
| No active likes | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0`, `data.checkpoint` |
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

A `cursor` issued before the checkpoint existed (it has no checkpoint
claim) responds 422 `invalid_cursor` as well: the client discards it and
restarts the read from the first page.

The `checkpoint` here serves to start a later `GET /likes/sync`, not to
continue this read's own walk.

A soft-deleted (unliked) row is excluded here; use `GET /likes/sync` to
see it.

Each like has `track_id`, `title`, `artists`, `album`, `album_id`,
`thumbnail_url`, `duration_seconds`, `created_at`, `updated_at` and
`deleted_at`. `thumbnail_url` is returned at 544 x 544 with smart crop, see
[Image size](conventions.md#image-size). `artists` is a non-empty list of objects with `id` and
`name`. `duration_seconds` and `deleted_at` can be null; no other field
can be null. `deleted_at` is always null in this endpoint's response.

`title`, `artists`, `album`, `album_id`, `thumbnail_url` and
`duration_seconds` are read from the shared track catalog (`public.tracks`)
through the like's relation, not from a copy stored per like: if the catalog
is refreshed, the like shows the new values.

## POST /likes

Likes a track. Idempotent and revives a previous unlike: upsert on
`(user_id, track_id)`, so liking a track the user already unliked clears
`deleted_at` and restores the row instead of erroring.

| Case | Status | Body |
|---|---|---|
| Track liked (new or revived) | 200 | `ok: true`, `data` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| `duration_seconds` omitted and the track is not in the catalog | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `track_id`, `title`, `artists` (non-empty list),
`album`, `album_id`, `thumbnail_url`. The track is written to the shared
catalog (upsert by `track_id`), refreshing any metadata already there with
what the client sends. `duration_seconds` is optional: if omitted and the
track is already in the catalog, the catalog's duration is kept; if omitted
and the track is not there, the request is 422 `invalid_request` and nothing
is written. The like is always scoped to the
caller's user id from the auth token; any `user_id` sent in the body is
ignored. The response returns `thumbnail_url` at 544 x 544 with smart crop
([Image size](conventions.md#image-size)); the catalog keeps the value sent.
The response is built from the like's row with the catalog track.

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
| Changes since `since` (first page) | 200 | `ok: true`, `data.items`, `data.page`, `data.checkpoint` |
| Next page via `cursor` | 200 | `ok: true`, `data.items`, `data.page`, `data.checkpoint` (the first page's; `since` ignored if also sent) |
| No changes since `since` | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.checkpoint` |
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
note on `GET /likes` for the general case. The same holds for a cursor
issued before the checkpoint existed: 422 `invalid_cursor`, and the client
restarts the sweep from `since`.

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

The server applies the overlap in the [checkpoint](#checkpoint), not in
`since`: using the checkpoint of the previous read as the next `since`
covers the writes whose `updated_at` was stamped up to 60 seconds before
the read began and committed after it. The resulting repeats are the same
idempotent upserts described above. This is still not a guarantee against
a transaction that stays open for more than 60 seconds.

The `cursor` belongs to one sweep. When starting a **new** sweep, the
client discards any `cursor` it kept and starts again from `since`.
Reusing an old `cursor` does not skip rows — the cursor wins over
`since`, so it only re-emits rows already seen — but it also will not
pick up the changes a fresh sweep from `since` would.

A metadata change in the track catalog does not move the like's `updated_at`,
so a sweep does not re-emit it: only like, unlike and re-like do.

## Checkpoint

`data.checkpoint` is on every page of `GET /likes` and `GET /likes/sync`,
the empty one included. It is the database clock at the start of the first
page of the read, minus 60 seconds, as an ISO-8601 string with a time
zone, for example `2026-10-03T23:59:50.161553+00:00`. It may carry
microseconds: the client uses it as is and does not reformat it.

All the pages of one read return exactly the same value: the first page
reads the clock, and the cursor carries it to the following pages.

The client sends it, unchanged, as `since` on its next
`GET /likes/sync`. It is sent back without reformatting, but
percent-encoded like any query value: the `+` of the offset goes as
`%2B` (`...%2B00:00`). Otherwise the `+` arrives as a space and the
response is 422 `invalid_request`. Because `since` is exclusive and the checkpoint is 60
seconds behind the start of the read, rows written just before the read
that committed after it are still returned. In `GET /likes` the checkpoint
serves to start a later sync, not to continue its own walk.

`since` itself is not modified by the server. A client that keeps
subtracting 60 seconds from the newest `updated_at` it received still
works, because `since` did not change; the checkpoint is the preferred way.

## Database access

Every query on this page runs on `get_user_db` (`core/auth.py`): the
caller's own JWT, not the service-role client. Supabase RLS applies as a
second barrier behind the explicit `.eq("user_id", ...)` filters already
in `services/likes_service.py` — neither replaces the other.

The exception is `POST /likes`: the duration read and the upsert into
`tracks` run on `get_db` (service-role), because `tracks` has no write
policy for `authenticated`, like `POST /playlists/{playlist_id}/tracks`.
The upsert of `user_likes` stays on `get_user_db`.

The checkpoint comes from the `likes_sync_checkpoint()` function (migration
`038`), called with `db.rpc` on the first page of each read. It runs on
`get_user_db` as `authenticated`; `anon` cannot execute it. It is called
before the likes query, and a failure of it is 502/504, never an invented
checkpoint.
