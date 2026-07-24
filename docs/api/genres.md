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
