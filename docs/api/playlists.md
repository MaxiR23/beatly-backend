## POST /playlists

Creates a playlist owned by the authenticated user.

| Case | Status | Body |
|---|---|---|
| Playlist created | 200 | `ok: true`, `data` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `title`, between 1 and 200 characters. Optional:
`description`, stored as given or null if omitted, and `is_public`,
false if omitted. `owner_id`, `created_at` and `updated_at` are
server-managed.

The playlist is always owned by the caller's user id from the auth
token; any `owner_id` sent in the body is ignored. Unknown fields are
ignored rather than rejected, unlike `PATCH /profile/me`, which returns
422 for them.

## GET /playlists

Lists the authenticated user's playlists, newest created first,
cursor-paginated. See the Pagination section of `conventions.md` for the
shared `limit`/`cursor` query params and the `data.items`/`data.page`
shape.

| Case | Status | Body |
|---|---|---|
| Playlists exist | 200 | `ok: true`, `data.items`, `data.page` |
| No playlists | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"playlists": [...]}` and an empty
result used to be `ok: false, reason: "no_playlists"`. Both are gone: an
empty result is now a normal empty first page (`ok: true`), per the
Pagination section of `conventions.md`. Like `no_likes`,
`no_library_items` and `no_recents`, `no_playlists` is deprecated
entirely — no endpoint returns it anymore.

There is no `sort` or `order`: the order is fixed, `created_at`
descending, with `id` breaking ties on playlists created at the same
instant. The sort key is `created_at` and not `updated_at` on purpose —
editing a playlist bumps `updated_at`, so ordering by it would move a
playlist mid-walk and make a paginated client skip or repeat rows.

Each playlist has `id`, `owner_id`, `title`, `description`, `is_public`,
`created_at` and `updated_at`. `description` can be null; no other field
can be null. A playlist stored with a null `is_public` — possible only
for rows predating this domain — reads back as false. This endpoint
returns the playlists themselves, without their tracks — use
`GET /playlists/{playlist_id}/tracks` for those.

## GET /playlists/{playlist_id}

Returns one playlist's metadata and aggregates.

| Case | Status | Body |
|---|---|---|
| Playlist found | 200 | `ok: true`, `data` |
| Playlist has no tracks | 200 | `ok: true`, `data.total_count: 0`, `data.total_duration_seconds: 0` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Malformed `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty playlist is `ok: true` with `total_count: 0` and
`total_duration_seconds: 0`, not an empty state: the playlist itself is
the payload here. Every playlist is empty right after it is created, so
an empty one is a normal result, not a missing resource.

**Breaking change:** `data.tracks` and `data.has_more` are gone —
tracks are no longer inline in the detail. Fetch them from
`GET /playlists/{playlist_id}/tracks` instead. `data` is now exactly a
playlist — the same fields as in `GET /playlists` — plus `total_count`
and `total_duration_seconds`. There is no `track_count`: the track count
is `total_count`.

`total_count` is how many tracks the playlist has, calculated by the
database. `total_duration_seconds` is the sum of `duration_seconds`
across every track in the playlist, also calculated by the database, not
derived from any track list. On a playlist with no tracks it is `0`,
never `null`.

Reading a playlist requires permission to edit it. A playlist that does
not exist and one owned by another user are both 404
`playlist_not_found`, so the response never confirms that someone else's
playlist exists. `is_public` has no effect on **this** endpoint: reading
a playlist here still requires edit permission, and a public playlist
owned by another user is still a 404. `is_public` does have an effect
elsewhere: a playlist with `is_public: true` is readable without a token
via `GET /public/playlists/{playlist_id}`, see `docs/api/public.md`.

## GET /playlists/{playlist_id}/tracks

Returns a playlist's tracks, cursor-paginated. See the Pagination section
of `conventions.md` for the shared `limit`/`cursor` query params and the
`data.items`/`data.page` shape.

| Case | Status | Body |
|---|---|---|
| Tracks returned | 200 | `ok: true`, `data.items`, `data.page` |
| Playlist has no tracks | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Malformed `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

The cursor is validated before the playlist is even looked up: an
invalid `cursor` is 422 `invalid_cursor` even for a playlist that does
not exist.

There is no `sort` or `order`: the order is fixed, the playlist's own
internal ordering column (never exposed itself), ascending, with the
`playlist_tracks` row's own id breaking ties on the rare tie.

Each item has `track_id`, `title`, `artists`, `album`, `album_id`,
`duration_seconds`, `thumbnail_url` and `position`. No field can be
null. There is **no `id`**: the internal catalog uuid never crosses this
boundary (`docs/api/conventions.md`, "Track identity") — a client that
used to read `id` off `data.tracks` on the old `GET /playlists/{id}`
loses that field migrating to `data.items` here. `track_id` is the
provider id the likes and activity domains key on, and the one to use
for playback, to check whether a track is liked, and to address a track
in the endpoints below. `artists` is a non-empty list of objects with
`id` and `name`.

`position` is the track's 1-based index **across the whole playlist**,
in this endpoint's order — not a per-page index. It is consecutive
between pages when nothing is written to the playlist between them: with
`limit=50`, the second page starts at `51`. It is calculated fresh on
every response — the first page's from the row's own index, a cursored
page's from a count of the rows at or before the cursor plus the row's
index within the page — never a stored value. It is **not a stable
identifier**: it changes whenever a track earlier in the playlist is
added, removed or moved, even though the track itself did not move. Use
`track_id` to identify a track across requests, never `position`.

Because this is keyset pagination over a column a reorder can rewrite,
walking a playlist while `POST .../move-track` runs against it is not
perfectly consistent: a track moved to behind an already-passed cursor
is skipped for the rest of that walk, and one moved to ahead of the
cursor is returned a second time. This is the limitation
`conventions.md`'s Pagination section accepts for any sort key a write
can change, not a bug. Separately, the preceding-rows count `position` is based
on is not read in the same transaction as the page itself, so a
concurrent add, remove or move landing between the two can shift a
page's `position` values by one.

Reading a playlist's tracks requires permission to edit the playlist,
the same as `GET /playlists/{playlist_id}`: an unknown playlist and one
owned by another user are both 404 `playlist_not_found`.

## GET /playlists/{playlist_id}/track-ids

Returns only the provider `track_id`s of a playlist's tracks, in order,
cursor-paginated, for a client building or shuffling a play queue without
holding the full track list or metadata it already has rendered. See the
Pagination section of `conventions.md` for the shared `limit`/`cursor`
query params and the `data.items`/`data.page` shape.

| Case | Status | Body |
|---|---|---|
| Track ids returned | 200 | `ok: true`, `data.items`, `data.page` |
| Playlist has no tracks | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Malformed `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`data.items` is `list[str]`: each element is directly the provider
`track_id`, not an object — no `title`, `artists`, `album`, `album_id`,
`duration_seconds`, `thumbnail_url` or `position`. The cursor is
validated before the playlist is even looked up, same as
`GET /playlists/{playlist_id}/tracks`.

The order and the sort key are exactly the same as
`GET /playlists/{playlist_id}/tracks` above (the playlist's own internal
ordering column, never exposed itself, ascending, with the
`playlist_tracks` row's own id breaking ties). Because it is the same
sort key, a `next_cursor` from that endpoint decodes and continues here,
and vice versa — they are the same cursor over the same rows, not a
different endpoint's. Walking either endpoint page by page returns the
same track ids in the same order.

Reading a playlist's track ids requires permission to edit the playlist,
the same as `GET /playlists/{playlist_id}/tracks`: an unknown playlist
and one owned by another user are both 404 `playlist_not_found`.

This endpoint carries no `position`, so it pays one query per page less
than `GET /playlists/{playlist_id}/tracks` — no preceding-rows count.
The keyset-pagination limitation `conventions.md`'s Pagination section
accepts for any sort key a write can change still applies here exactly
as on `GET /playlists/{playlist_id}/tracks`: walking this endpoint while
`POST .../move-track` runs against the same playlist can skip a moved
track or return it twice.

## GET /playlists/liked

Returns the authenticated user's liked-tracks virtual playlist's
metadata and aggregates, so a client can render it with the same
component it uses for a real playlist, with no conditional logic.

| Case | Status | Body |
|---|---|---|
| Liked playlist returned | 200 | `ok: true`, `data` |
| No liked tracks | 200 | `ok: true`, `data.total_count: 0`, `data.total_duration_seconds: 0` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

There is no 404 and no "malformed `playlist_id`" case here: for an
authenticated user this playlist always exists, even when it is empty —
unlike `GET /playlists/{playlist_id}`, it has no parent row that can be
missing or owned by someone else.

**Breaking change:** `data.tracks` and `data.has_more` are gone, exactly
as on `GET /playlists/{playlist_id}`. Fetch the tracks from
`GET /playlists/liked/tracks` instead. `data` has exactly the shape of
`GET /playlists/{playlist_id}`: the same `Playlist` fields plus
`total_count` and `total_duration_seconds`, both calculated by the
database over every active like — `total_duration_seconds` from
`get_liked_tracks_duration_total`, not summed in Python.

`id` and `title` are both the literal `"liked"`. **`title` is an
identifier here, not a display string**: the backend does not impose a
language on it, and the client resolves the visible name (e.g. "Liked
Songs") with its own i18n.

`owner_id` is the caller's user id from the token. `is_public` is always
`false` and `description` is always `null`. This playlist is not
editable: it does not accept `PATCH`, `DELETE` or any of the
`/playlists/{playlist_id}/tracks...` endpoints, all of which require a
real uuid and would reject `"liked"` with 422 `invalid_request`.

`created_at` is the `created_at` of the caller's oldest active like, and
`updated_at` is the `created_at` of their most recent one — not the time
of the request, so a client can cache the response. With zero active
likes, both are the time of the request instead.

## GET /playlists/liked/tracks

Returns the authenticated user's liked tracks, cursor-paginated, in the
same shape as `GET /playlists/{playlist_id}/tracks`.

| Case | Status | Body |
|---|---|---|
| Tracks returned | 200 | `ok: true`, `data.items`, `data.page` |
| No liked tracks | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

There is no 404 and no "malformed `playlist_id`" case, the same reason
as `GET /playlists/liked` above.

Tracks are ordered by `user_likes.created_at` ascending (oldest like
first), with `track_id` breaking ties — the same order `GET /likes`
uses, so a cursor from `GET /likes` is also valid here: it is the same
sort key over the same rows, not a different endpoint's cursor. A like
has no position of its own, but unliking and re-liking a track does not
move it: the re-like reactivates the same row and keeps its original
`created_at`, so the track returns to the position it already had, not
to the end of the list.

Item shape and `position` are exactly as described on
`GET /playlists/{playlist_id}/tracks` above. Of that endpoint's two
pagination caveats, only the non-atomic preceding count applies here: a
concurrent like or unlike landing between the count and the page can
shift a page's `position` values by one. The reorder caveat does not
apply, because no write changes a like's sort key — there is no move,
and a re-like keeps its original `created_at`, as above.

## GET /playlists/liked/track-ids

Returns only the provider `track_id`s of the authenticated user's liked
tracks, in order, cursor-paginated, in the same shape as
`GET /playlists/{playlist_id}/track-ids`.

| Case | Status | Body |
|---|---|---|
| Track ids returned | 200 | `ok: true`, `data.items`, `data.page` |
| No liked tracks | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

There is no 404 and no "malformed `playlist_id`" case, the same reason
as `GET /playlists/liked` above.

`data.items` is `list[str]` of bare provider `track_id`s, same as
`GET /playlists/{playlist_id}/track-ids`. The order and sort key are the
same as `GET /playlists/liked/tracks` (`user_likes.created_at` ascending
with `track_id` breaking ties) — the same order `GET /likes` uses. A
cursor is interchangeable between this endpoint, `GET /playlists/liked/tracks`
and `GET /likes`: all three share the same sort key over the same rows.

Unlike `GET /playlists/{playlist_id}/track-ids`, this endpoint never
queries the track catalog: `user_likes.track_id` is already the provider
id, so there is nothing to resolve. It has no reorder caveat either —
no write changes a like's sort key, the same reasoning as
`GET /playlists/liked/tracks` — and no preceding-count caveat, since this
endpoint carries no `position`.

## PATCH /playlists/{playlist_id}

Updates a playlist's `title`, `description` or `is_public`.

| Case | Status | Body |
|---|---|---|
| Playlist updated | 200 | `ok: true`, `data` |
| Empty body | 200 | `ok: true`, `data` unchanged |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

The update is partial: only the fields present in the body are written,
and an omitted field is left untouched. Sending `"description": null`
clears the description, since `description` is nullable. Sending
`"title": null` is a 422 — `title` is required on a playlist, so leaving
it unchanged means omitting it. A blank or over-200-character title is
also a 422. An empty body `{}` writes nothing and returns the playlist
as it stands.

`owner_id`, `created_at` and `updated_at` cannot be set by the client;
`updated_at` is bumped by the database. Only `title`, `description` and
`is_public` are editable — adding or reordering tracks is not part of
this endpoint. Setting `is_public: true` publishes the title, the
description, the full track list and the thumbnail mosaic to anyone who
knows the playlist's uuid, with no token, via
`GET /public/playlists/{playlist_id}` — see `docs/api/public.md`.
Setting it back to `false` reverts that.

An unknown playlist and one owned by another user are both 404
`playlist_not_found`, never a 500.

## DELETE /playlists/{playlist_id}

Deletes a playlist and, by cascade, its track entries and any library
items referencing it. Not idempotent: deleting an already-deleted
playlist is a 404.

| Case | Status | Body |
|---|---|---|
| Playlist deleted | 200 | `ok: true` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Malformed `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Deleting requires permission to edit, so a user can only delete
playlists they own. As everywhere else in this domain, a playlist that
does not exist and one owned by another user are indistinguishable: both
404 `playlist_not_found`, never a 500.

## POST /playlists/{playlist_id}/tracks

Adds one track to the end of a playlist. Not idempotent: adding a track
the playlist already has is a 409, not a second copy.

| Case | Status | Body |
|---|---|---|
| Track added | 200 | `ok: true`, `data` |
| Already in the playlist | 409 | `ok: false`, `reason: "track_already_in_playlist"` |
| Concurrent writes kept colliding | 409 | `ok: false`, `reason: "order_key_conflict"` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `track_id`, `title`, `artists` (non-empty list),
`album`, `album_id`, `thumbnail_url` and `duration_seconds`. There are no
optional ones. `position` is not sent; it is calculated.

`duration_seconds` is required here, unlike on `POST /likes` where it is
optional: a playlist track is read back through
`GET /playlists/{id}/tracks`, which cannot serialize a null duration, and
nothing enriches the track from the catalog afterwards. Sending the body
without it is a 422 rather than a row that breaks the read later.

The metadata is written to the shared track catalog, keyed on
`track_id`, so adding a track the catalog already has refreshes its
metadata instead of duplicating it. The response is the stored track,
including its `position` and the catalog `id`.

A new track always lands at the end of the playlist. `position` in the
response is that track's 1-based index — equivalently, the playlist's
track count right after the insert — calculated by the database under
the same lock as the write itself, so two clients adding to the same
playlist at once cannot be given the same `position`, and the second of
two concurrent adds of the same track gets the 409 rather than a
duplicate entry. That `position` is the same global index
`GET /playlists/{playlist_id}/tracks` gives that track immediately
afterwards, and equal to the `total_count`
`GET /playlists/{playlist_id}` gives immediately afterwards too.

Placing the track in playlist order is not fully atomic with the write:
the service retries internally, up to 3 attempts, re-reading the
playlist before each one, if that placement collides with a concurrent
write to the same playlist. Retrying is invisible to the caller on
success. If every attempt still collides, nothing was written and the
response is 409 `order_key_conflict` — the request can simply be sent
again.

## POST /playlists/{playlist_id}/tracks/bulk

Adds many tracks in one request, ignoring the ones already there.
Idempotent, unlike adding a single track: nothing already in the playlist
is a conflict here, it is just skipped.

| Case | Status | Body |
|---|---|---|
| Batch processed | 200 | `ok: true`, `data.added`, `data.skipped` |
| Concurrent writes kept colliding | 409 | `ok: false`, `reason: "order_key_conflict"` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

The body is `{"tracks": [...]}`, where each entry has exactly the fields
`POST /playlists/{playlist_id}/tracks` requires. Between 1 and 200 tracks
per batch — an empty list and a 201st track are both 422, decided before
anything is written, so a rejected batch never lands halfway. A client
with more tracks than that sends more than one request; this is a batch
cap, not pagination.

The response is `added` and `skipped`, which together always equal the
number of tracks sent. `skipped` merges three cases that need no
distinction from the caller: a track repeated inside the batch, which is
added once and skipped for the rest, a track already in the playlist,
and a track another request adds while this batch is running. A batch
where nothing is left to add is `added: 0`, not an error.

Added tracks are appended in the order they were sent. This endpoint's
response carries no `position`; the added tracks' `position` in a later
`GET /playlists/{playlist_id}/tracks` is their global 1-based index,
consecutive with no gaps regardless of which tracks in the batch were
skipped.

The whole batch is linked in one statement, so it either lands complete
or not at all: a database failure is a 502 with nothing added, never a
partial count. The catalog metadata is written first, in a separate
statement, for every distinct track in the batch — including the ones
that turn out to be already in the playlist. If the link then fails, that
metadata write stands; no playlist changed, and the catalog is shared
between all of them.

Re-sending the same batch is safe — what landed the first time is
skipped the second.

Placing the batch in playlist order retries internally on a collision
with a concurrent write, the same way and with the same 409
`order_key_conflict` once exhausted as `POST /playlists/{playlist_id}/tracks`
— see that endpoint's description of the retry.

## DELETE /playlists/{playlist_id}/tracks/{track_id}

Removes a track from a playlist. Idempotent — removing a track that is
not in the playlist is still a 200, like `DELETE /likes/{track_id}` and
unlike `DELETE /playlists/{playlist_id}`.

| Case | Status | Body |
|---|---|---|
| Removed (or not there) | 200 | `ok: true` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Malformed `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`track_id` is the provider id, the same one `POST .../tracks` takes and
the one in the `track_id` field of a track — not the catalog uuid in its
`id` field. A `track_id` the catalog has never seen is a 200 as well:
it is certainly not in the playlist, which is the state the caller
asked for. The response body carries no count; how many links were
removed, one or none, is not a distinction the caller needs.

The track stays in the catalog, since other playlists and other users
reference it. Only the link is removed. The remaining tracks' `position`
in the next `GET /playlists/{playlist_id}/tracks` shift down by one past
the point of the removed track, because `position` is that endpoint's
own global 1-based index — there is no gap to leave or to close.

## POST /playlists/{playlist_id}/move-track

Moves a track to a different place in the playlist.

| Case | Status | Body |
|---|---|---|
| Track moved | 200 | `ok: true` |
| Concurrent writes kept colliding | 409 | `ok: false`, `reason: "order_key_conflict"` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Position out of range | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `old_position` and `new_position`, both 1-based and both
between 1 and the number of tracks in the playlist — the same global
`position` `GET /playlists/{playlist_id}/tracks` returns for a track.
This endpoint's response carries no data of its own; the moved track
moves, and every track's `position` in the next
`GET /playlists/{playlist_id}/tracks` reflects the new order.

A position below 1 or past the end of the playlist is a 422, and any
move in an empty playlist is a 422. The reorder is rejected rather than
adjusted: silently clamping an out-of-range index would report a move
that put the track somewhere else.

Placing the moved track in playlist order retries internally on a
collision with a concurrent write, the same way and with the same 409
`order_key_conflict` once exhausted as `POST /playlists/{playlist_id}/tracks`
— see that endpoint's description of the retry.

## GET /playlists/owned-with-track/{track_id}

Returns the ids of the caller's playlists that contain a given track, for
a client showing which playlists a song is already in.

| Case | Status | Body |
|---|---|---|
| Playlists found | 200 | `ok: true`, `data.playlist_ids` |
| In none of them | 200 | `ok: true`, `data.playlist_ids: []` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`track_id` is the provider id, as in the endpoints above.

A track in none of the caller's playlists is `ok: true` with an empty
list, not `ok: false`, `no_playlists`. This is a membership question, and
"in none of them" is the answer to it rather than an absence of data —
the same reasoning as an empty `GET /likes/sync` window.
`no_playlists` is deprecated entirely and no endpoint returns it.

Only playlists the caller owns are considered, so this never reveals that
someone else's playlist contains the track. A track that does not exist
and one in no playlist are indistinguishable, both an empty list.
