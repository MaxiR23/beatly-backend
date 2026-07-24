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