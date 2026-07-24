## GET /genres

Lists all genres, ordered for display.

| Case | Status | Body |
|---|---|---|
| Genres exist | 200 | `ok: true`, `data.genres` |
| No genres exist | 200 | `ok: false`, `reason: "no_genres"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty list is not an error. See `conventions.md`.

Each genre has `slug`, `name` and `description`. `description` can be
null. Navigate by `slug`.

## GET /genres/{slug}/playlists

Lists the curated playlists of a genre, ordered for display.

| Case | Status | Body |
|---|---|---|
| Genre exists, has playlists | 200 | `ok: true`, `data.playlists` |
| Genre exists, no playlists | 200 | `ok: false`, `reason: "no_playlists"` |
| Genre does not exist | 404 | `ok: false`, `reason: "genre_not_found"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty playlist list is not an error. See `conventions.md`. The
genre lookup happens first, so a bad slug is distinguishable from a
genre with no playlists.

Each playlist has `id`, `title`, `description`, `thumbnail_url`,
`track_count` and `category`. `description`, `thumbnail_url` and
`category` can be null.

## GET /genres/{slug}/categories

Lists the distinct categories used by a genre's playlists, sorted, for
building filters.

| Case | Status | Body |
|---|---|---|
| Genre exists, has categories | 200 | `ok: true`, `data.categories` |
| Genre exists, no categories | 200 | `ok: false`, `reason: "no_categories"` |
| Genre does not exist | 404 | `ok: false`, `reason: "genre_not_found"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty category list is not an error. See `conventions.md`. The
genre lookup happens first, so a bad slug is distinguishable from a
genre with no categories. Null categories on individual playlists are
discarded, not surfaced.

## GET /genre-playlists/{playlist_id}/tracks

Lists the tracks of a curated playlist, in order.

| Case | Status | Body |
|---|---|---|
| Playlist exists, has tracks | 200 | `ok: true`, `data.tracks` |
| Playlist exists, no tracks | 200 | `ok: false`, `reason: "no_tracks"` |
| Playlist does not exist | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

An empty track list is not an error. See `conventions.md`. The
playlist lookup happens first, so a bad `playlist_id` is
distinguishable from a playlist with no tracks.

Each track has `track_id`, `title`, `artists`, `album`, `album_id`,
`duration_seconds`, `thumbnail_url` and `position`. `artists` is a
list of objects with `id` and `name`. None of these fields can be
null. `position` reflects the track's order within the playlist, and
the list is returned sorted by it.

The endpoint returns up to 500 tracks per playlist. Curated playlists
are expected to hold tens of tracks, not thousands; this is an
explicit cap, not pagination.
