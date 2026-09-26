## GET /library

Lists the authenticated user's unified library — a fixed "liked songs"
entry, the user's own playlists, and their saved albums/playlists — in
one cursor-paginated list. See the Pagination section of
`conventions.md` for the shared `limit`/`cursor` query params and the
`data.items`/`data.page` shape; this endpoint departs from that section
in one way, noted below.

| Case | Status | Body |
|---|---|---|
| Library has entries | 200 | `ok: true`, `data.items`, `data.page` |
| Library is empty | 200 | `ok: true`, `data.items: [<liked entry>]`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Order is fixed: newest entry first, by the entry's own date
(`playlists.created_at` for an own playlist, `library_items.added_at`
for a saved item), mixed across both sources — an own playlist can sit
between two saved items, or the other way around.

The first page (no cursor) always starts with a fixed entry:

    {"kind": "playlist", "id": "liked", "title": "liked",
     "thumbnail_url": null, "subtitle": null, "source": "liked"}

It is the same "liked songs" playlist `GET /playlists/liked` opens, it
always exists — even with zero likes and an otherwise empty library —
and it does not count against `limit` or `data.page.total`: the first
page can carry up to `limit + 1` items (the fixed entry plus up to
`limit` rows from the rest of the library), and `data.page.total` counts
only the user's own playlists plus their saved items, never the fixed
entry. It never appears on a page reached through `cursor`. An empty
`cursor` (`?cursor=`) is the absent cursor, as on every paginated
endpoint: it gets the first page, fixed entry included. Because of it,
an empty library is not `data.items: []` as the Pagination section
of `conventions.md` describes for every other endpoint — it is
`data.items: [<the fixed entry above>]`.

Each entry has `kind` (`"album"` or `"playlist"`), `id`, `title`,
`thumbnail_url`, `subtitle` and `source`. `kind`, `id` and `title` are
never null; `thumbnail_url` and `subtitle` can be. `source` says where
the entry came from and, together with `id`, where it opens:

| `source` | Opens at | `id` is |
|---|---|---|
| `liked` | `GET /playlists/liked` | always `"liked"` |
| `user` | `GET /playlists/{id}` | the playlist's own uuid |
| `genre`, `replay`, `presenting`, `external` | same as before this change | the saved item's external id |

`thumbnail_url` for an own playlist (`source: "user"`) is the thumbnail
of the first track, in playlist order, that has one; `null` if the
playlist is empty or no track in it has a thumbnail. `subtitle` is the
artist for a saved album, the creator for a saved playlist, and always
`null` for an own playlist and for the fixed entry — both are the
caller's own, the same reasoning that already keeps liked songs without
one. No internal row id is exposed.

An own playlist is never a saved item: there is nothing to save about
your own, so this endpoint assumes `POST /library` never receives one
and does not deduplicate the two branches of the view against each
other. A playlist saved that way would appear twice, once as `source:
"user"` and once as a saved item. Decided in #153, not an oversight.

**Breaking change:** `data` used to be `{"items": [...]}` and an empty
result used to be `ok: false, reason: "no_library_items"`. Both are gone:
an empty result is now a normal empty first page (`ok: true`), per the
Pagination section of `conventions.md`. `no_library_items` is deprecated
and no longer returned by this endpoint.

**Breaking change (#153):** this endpoint now unifies own playlists and
the fixed "liked songs" entry into what used to be only saved
albums/playlists, with effects on every part of the response:

- Item shape: an item was `kind`, `external_id`, `title`,
  `thumbnail_url`, `artist`, `artist_id`, `album_id`, `album_name`,
  `source`, `added_at`, `updated_at`. It is now `kind`, `id`, `title`,
  `thumbnail_url`, `subtitle`, `source`. `external_id`, `artist`,
  `artist_id`, `album_id`, `album_name`, `added_at` and `updated_at` are
  gone; `id` and `subtitle` are new. `source` gains two new values,
  `"liked"` and `"user"`. A `thumbnail_url` that used to come back as
  `""` for a saved item now comes back `null`.
- `sort` and `order` query params are gone. A client that still sends
  them gets the fixed order above instead of a 422: FastAPI ignores
  unknown query params by default, so neither is rejected anymore.
- A `cursor` emitted under the removed `sort=title` or `order=asc` is
  now 422 `invalid_cursor` — that ordering no longer exists. A `cursor`
  emitted under the previous default (`added_at` descending) still
  decodes, because the sort key's tag did not change, and continues
  from the same position over the unified list — but that continuation
  never includes the fixed entry or any own playlist newer than the
  cursor's position, because neither existed in the list it was
  originally paginating over. A client that wants to see the whole
  library goes back to the first page instead.
- The fixed "liked songs" entry, described above.
- The empty-library body, described above (`data.items: [<liked
  entry>]`, not `[]`).

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

## Database access

Every query on this page runs on `get_user_db` (`core/auth.py`): the
caller's own JWT, not the service-role client.

`GET /library` reads `public.library_entries` (`035`), a view created
with `security_invoker = true` so that RLS on `playlists`,
`library_items`, `playlist_tracks` and `tracks` applies as the caller,
not as the view's owner. For the `library_items` side this is a real
second barrier behind the explicit `.eq("user_id", ...)` filter already
in `services/library_service.py`. For the `playlists` side it is not:
Supabase RLS on `playlists` also allows any *public* playlist regardless
of owner, so the `.eq("user_id", ...)` filter is what limits that branch
to the caller's own playlists — RLS alone would also let public
playlists from other users through.

`POST /library` and `DELETE /library/{kind}/{external_id}` are
unchanged: both still query `library_items` directly, with Supabase RLS
as a second barrier behind the explicit `.eq("user_id", ...)` filters
already in `services/library_service.py` — neither replaces the other.
