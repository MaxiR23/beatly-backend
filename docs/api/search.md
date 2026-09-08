## GET /search

Searches the external provider for artists, songs and albums that match
a query, in a single response. Authenticated: requires a Supabase JWT
like the rest of the API.

| Case | Status | Body |
|---|---|---|
| Results found | 200 | `ok: true`, `data.artist`, `data.songs`, `data.albums` |
| No result matches the query | 200 | `ok: true`, `data.artist: null`, `data.songs: []`, `data.albums: []` |
| Missing or empty `q` | 422 | `ok: false`, `reason: "invalid_request"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| The external provider failed, including a response whose layout could not be parsed | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`q` is the only query parameter, and it is required (`min_length=1`).
This endpoint does not accept `limit` or `cursor`: `songs` and `albums`
are returned whole, exactly as the external provider's filtered search
returns them, with no client-side truncation or pagination. This is the
same reasoning `docs/api/playlists.md` documents for `tracks` on
`GET /playlists/{playlist_id}`: the search result (artist + songs +
albums) is the payload of this endpoint, not a growable collection in
its own right, so it is not wrapped in the `Paginated[T]` envelope.

A search with no matches is a normal, successful result, not an empty
state with a `reason`: `data.artist` is `null` and `data.songs`/
`data.albums` are empty lists, with `ok: true`.

`data.artist` is the first result of a search filtered to artists
(`limit` 1), or `null` if none matches the query. When it is not null,
the elements of `data.songs` and `data.albums` whose `artists` include
its `id` come first, in the order the provider returned them; the rest
follow, also in their original order. Nothing is ever dropped: an item
that does not belong to the primary artist still appears, just after
the ones that do. Membership is decided only by comparing `id`, never
`name`.

Fields:

- `data.artist`: `id`, `name`. Both present when not null.
- Each element of `data.songs`: `track_id`, `title`, `artists`, `album`,
  `album_id`, `duration_seconds`, `thumbnail_url`.
- Each element of `data.albums`: `id` (the provider's `browseId`),
  `playlist_id` (the provider's `playlistId`), `title`, `artists`,
  `year` (nullable), `thumbnail_url` (nullable).
- `artists`, on both `songs` and `albums`, is a list of `{id, name}`
  with at least one element, but `id` inside it is nullable: the
  external provider can mention an artist without a link, and that
  element arrives with `name` but `id: null`. An artist with a null id
  never matches the primary artist, no matter what, so it always falls
  into the second group described above — it is never dropped.
