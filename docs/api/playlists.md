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

Lists the authenticated user's playlists, newest created first.

| Case | Status | Body |
|---|---|---|
| Playlists exist | 200 | `ok: true`, `data.playlists` |
| No playlists | 200 | `ok: false`, `reason: "no_playlists"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty playlist list is not an error. See `conventions.md`. Note that
`no_playlists` is also returned by `GET /genres/{slug}/playlists` for a
genre with no curated playlists; the two are unrelated.

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
*is* the payload and an empty one is `ok: false`, `no_tracks`. Every
playlist is empty right after it is created, so an empty one is a normal
result, not a missing resource.

The response is a playlist — same fields as in `GET /playlists` — plus
`tracks`, `total_count` and `has_more`. `tracks` is capped at 1000
entries. `total_count` is how many tracks the playlist actually has and
`has_more` is true when the list was cut, so a long playlist is never
truncated silently. This is an explicit cap, not pagination; real
pagination is deferred to issue #40, opened for the likes endpoints.

Each track has `id`, `track_id`, `title`, `artists`, `album`,
`album_id`, `duration_seconds`, `thumbnail_url` and `position`. No field
on a track can be null. `id` is the catalog uuid, the id the playlist
track endpoints address; `track_id` is the provider id the likes and
activity domains key on, and the one to use for playback or to check
whether a track is liked. `artists` is a non-empty list of objects with
`id` and `name`.

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
