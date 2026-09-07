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
`GET /playlists/{playlist_id}` for those.

## GET /playlists/{playlist_id}

Returns one playlist with its tracks, ordered by position.

| Case | Status | Body |
|---|---|---|
| Playlist found | 200 | `ok: true`, `data` |
| Playlist has no tracks | 200 | `ok: true`, `data.tracks: []` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Malformed `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

A playlist with no tracks is `ok: true` with an empty `tracks` list, not
an empty state: the playlist itself is the payload here. This differs
from `GET /genre-playlists/{playlist_id}/tracks`, where the track list
*is* the payload and an empty one is an empty first page -- `items: []`
with `has_more: false`. Every playlist is empty right after it is
created, so an empty one is a normal result, not a missing resource.

The response is a playlist — same fields as in `GET /playlists` — plus
`tracks`, `total_count` and `has_more`. `tracks` is capped at 1000
entries. `total_count` is how many tracks the playlist actually has and
`has_more` is true when the list was cut, so a long playlist is never
truncated silently. This is an explicit cap, not pagination:
paginating a playlist's track list is its own issue. The `limit`/`cursor`
params of `GET /playlists` do not apply here.

Each track has `id`, `track_id`, `title`, `artists`, `album`,
`album_id`, `duration_seconds`, `thumbnail_url` and `position`. No field
on a track can be null. `id` is the catalog uuid, which the playlist
stores internally; `track_id` is the provider id the likes and activity
domains key on, and the one to use for playback, to check whether a
track is liked, and to address a track in the endpoints below — a client
never needs the catalog uuid. `artists` is a non-empty list of objects
with `id` and `name`.

Reading a playlist requires permission to edit it. A playlist that does
not exist and one owned by another user are both 404
`playlist_not_found`, so the response never confirms that someone else's
playlist exists. `is_public` is stored but has no effect in this
version: there is no public read path yet, and a public playlist owned
by another user is still a 404.

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
this endpoint.

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
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `track_id`, `title`, `artists` (non-empty list),
`album`, `album_id`, `thumbnail_url` and `duration_seconds`. There are no
optional ones. `position` is server-managed.

`duration_seconds` is required here, unlike on `POST /likes` where it is
optional: a playlist track is read back through `GET /playlists/{id}`,
which cannot serialize a null duration, and nothing enriches the track
from the catalog afterwards. Sending the body without it is a 422 rather
than a row that breaks the read later.

The metadata is written to the shared track catalog, keyed on
`track_id`, so adding a track the catalog already has refreshes its
metadata instead of duplicating it. The response is the stored track,
including its `position` and the catalog `id`.

Positions start at 1 and a new track takes the highest one in the
playlist plus one. Removing a track renumbers the ones after it, so the
sequence closes up rather than leaving a hole.

The position is assigned by the database in the same statement that
writes the link, so two clients adding to the same playlist at once
cannot be given the same position, and the second of two concurrent adds
of the same track gets the 409 rather than a duplicate entry.

## POST /playlists/{playlist_id}/tracks/bulk

Adds many tracks in one request, ignoring the ones already there.
Idempotent, unlike adding a single track: nothing already in the playlist
is a conflict here, it is just skipped.

| Case | Status | Body |
|---|---|---|
| Batch processed | 200 | `ok: true`, `data.added`, `data.skipped` |
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

Added tracks are appended in the order they were sent, starting from the
highest existing position plus one, and their positions are contiguous
even when some tracks in the batch were skipped.

The whole batch is linked in one statement, so it either lands complete
or not at all: a database failure is a 502 with nothing added, never a
partial count. The catalog metadata is written first, in a separate
statement, for every distinct track in the batch — including the ones
that turn out to be already in the playlist. If the link then fails, that
metadata write stands; no playlist changed, and the catalog is shared
between all of them.

Re-sending the same batch is safe — what landed the first time is
skipped the second.

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
reference it. Only the link is removed. The remaining tracks close the
gap the removed one left, so the positions stay contiguous.

## POST /playlists/{playlist_id}/move-track

Moves a track to a different place in the playlist and renumbers the
rest.

| Case | Status | Body |
|---|---|---|
| Track moved | 200 | `ok: true` |
| Unknown or not editable | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Position out of range | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Required fields: `old_position` and `new_position`, both 1-based and both
between 1 and the number of tracks in the playlist. The renumbering
itself is done by the database.

A position below 1 or past the end of the playlist is a 422, and any
move in an empty playlist is a 422. The reorder is rejected rather than
adjusted: silently clamping an out-of-range index would report a move
that put the track somewhere else.

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
