## GET /genres

Lists all genres, ordered for display, using the paginated response
shape (`data.items` + `data.page`, see the Pagination section of
`conventions.md`). This endpoint does **not** accept `limit` or
`cursor`: see the note below.

| Case | Status | Body |
|---|---|---|
| Genres exist | 200 | `ok: true`, `data.items`, `data.page` |
| No genres exist | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"genres": [...]}` and an empty
result used to be `ok: false, reason: "no_genres"`. Both are gone: an
empty result is now a normal empty first page (`ok: true`), per the
Pagination section of `conventions.md`. `no_genres` is deprecated and no
longer returned by this endpoint.

Each genre has `slug`, `name` and `description`. `description` can be
null. Navigate by `slug`. Items are ordered by `sort_order` ascending.

## GET /genres/{slug}/playlists

Lists the curated playlists of a genre, ordered for display, using the
paginated response shape (`data.items` + `data.page`). This endpoint
does **not** accept `limit` or `cursor`: see the note below.

| Case | Status | Body |
|---|---|---|
| Genre exists, has playlists | 200 | `ok: true`, `data.items`, `data.page` |
| Genre exists, no playlists | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Genre does not exist | 404 | `ok: false`, `reason: "genre_not_found"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"playlists": [...]}` and an
empty result used to be `ok: false, reason: "no_playlists"`. Both are
gone: an empty result is now a normal empty first page (`ok: true`), per
the Pagination section of `conventions.md`. `no_playlists` is deprecated
and no longer returned by any endpoint.

The genre lookup happens first, so a bad slug is distinguishable from a
genre with no playlists.

Each playlist has `id`, `title`, `description`, `thumbnail_url`,
`track_count` and `category`. `description`, `thumbnail_url` and
`category` can be null. Items are ordered by `sort_order` ascending.

## GET /genres/{slug}/categories

Lists the distinct categories used by a genre's playlists, sorted, for
building filters, using the paginated response shape (`data.items` +
`data.page`). This endpoint does **not** accept `limit` or `cursor`:
see the note below.

| Case | Status | Body |
|---|---|---|
| Genre exists, has categories | 200 | `ok: true`, `data.items`, `data.page` |
| Genre exists, no categories | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Genre does not exist | 404 | `ok: false`, `reason: "genre_not_found"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"categories": [...]}` and an
empty result used to be `ok: false, reason: "no_categories"`. Both are
gone: an empty result is now a normal empty first page (`ok: true`), per
the Pagination section of `conventions.md`. `no_categories` is
deprecated and no longer returned by this endpoint.

The genre lookup happens first, so a bad slug is distinguishable from a
genre with no categories. Null categories on individual playlists are
discarded, not surfaced. `data.items` is `list[str]`, deduplicated and
sorted alphabetically.

## GET /genre-playlists/{playlist_id}/tracks

Lists the tracks of a curated playlist, in order, using the paginated
response shape (`data.items` + `data.page`). This endpoint does **not**
accept `limit` or `cursor`: see the note below.

| Case | Status | Body |
|---|---|---|
| Playlist exists, has tracks | 200 | `ok: true`, `data.items`, `data.page` |
| Playlist exists, no tracks | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Playlist does not exist | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Invalid `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"tracks": [...]}` and an empty
result used to be `ok: false, reason: "no_tracks"`. Both are gone: an
empty result is now a normal empty first page (`ok: true`), per the
Pagination section of `conventions.md`. `no_tracks` is deprecated and no
longer returned by this endpoint.

The playlist lookup happens first, so a bad `playlist_id` is
distinguishable from a playlist with no tracks.

Each track has `track_id`, `title`, `artists`, `album`, `album_id`,
`duration_seconds`, `thumbnail_url` and `position`. `artists` is a
list of objects with `id` and `name`. None of these fields can be
null. `position` reflects the track's order within the playlist, and
the list is returned sorted by it, ascending.

This endpoint used to cap the response at 500 tracks. **That cap is
gone.** A genre playlist is played, so the client needs every track for
its queue and shuffle; a limit the client cannot page through would only
offer a silent truncation with no way to get the rest. Curated
playlists are expected to hold tens of tracks (the largest today holds
120), so this is not paginated either. If these collections grow enough
that returning them whole stops being practical, real pagination gets
decided in its own issue instead of a silent cap.

## `limit`/`cursor` on these four endpoints

Unlike every other paginated list endpoint in this API, none of these
four declares the shared `limit`/`cursor` query params from the
Pagination section of `conventions.md`. Each one always returns the
entire collection in a single page: `page.limit` and `page.total` equal
the number of items returned (`0` when the collection is empty),
`page.has_more` is always `false` and `page.next_cursor` is always
`null`. This holds by construction, not because these collections
happen to be small today.

One caveat the API cannot enforce: with no cap of its own, the ceiling
on a response is PostgREST's `max-rows`, which is **1000** for this
project (verified in the Supabase dashboard, Data API settings). A
collection past 1000 rows would be truncated silently server-side:
`page.total` would report the truncated count as the whole collection
and `page.has_more` would still be `false`, so neither the client nor
this API could tell a full collection from a cut one.

This does not apply today -- the largest of these collections is a genre
playlist with 120 tracks -- but 1000 is the number at which returning
these lists whole stops being safe, and the point where real pagination
has to be decided in its own issue.

Sending `?limit=...` or `?cursor=...` to any of the four is not an
error: FastAPI ignores query params a route does not declare, so the
response is the same full page it would have been without them. There
is no `invalid_limit` or `invalid_cursor` case for these endpoints.
