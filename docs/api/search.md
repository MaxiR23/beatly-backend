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
| The external provider failed, including a response whose layout could not be parsed, or a songs or albums list in which every result was unreadable (a single unreadable result is omitted instead, see Malformed results) | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

A successful response may be served from a server-side Redis cache and be
up to **1 hour** stale relative to the external provider. The cache key
is derived from `q` (lowercased, trimmed, whitespace-collapsed, cut to
the first 200 characters and hashed), so this staleness applies per
normalized query text, not per raw query string, and two queries that
share their first 200 normalized characters share one cache entry. The
cache never changes the response's shape, status or reason, and a Redis
failure is invisible to the client: the endpoint responds with the same
body, status and reason as it would with no cache at all. A response
cached before `data.artist.thumbnail_url` existed returns
`thumbnail_url: null` for the artist until it expires (1 hour at most). See
`docs/adr/005-provider-cache-lives-in-the-services.md` for why.

The successful response carries `Cache-Control: max-age=<seconds left>`,
at most the operation's TTL (1 hour); see [Caching headers](conventions.md#caching-headers).

`q` is the only query parameter, and it is required (`min_length=1`).
This endpoint does not accept `limit` or `cursor`: `songs` and `albums`
are returned whole, exactly as the external provider's filtered search
returns them (minus any unreadable result, see Malformed results), with
no client-side truncation or pagination. The search
result (artist + songs + albums) is the payload of this endpoint, not a
growable collection in its own right, so it is not wrapped in the
`Paginated[T]` envelope.

A search with no matches is a normal, successful result, not an empty
state with a `reason`: `data.artist` is `null` and `data.songs`/
`data.albums` are empty lists, with `ok: true`.

`data.artist` is the first result of a search filtered to artists
(`limit` 1), or `null` if none matches the query. When it is not null,
the elements of `data.songs` and `data.albums` whose `artists` include
its `id` come first, in the order the provider returned them; the rest
follow, also in their original order. The ordering never drops anything: an
item that does not belong to the primary artist still appears, just
after the ones that do (a result omitted as malformed is omitted before
the ordering, see Malformed results). Membership is decided only by comparing `id`, never
`name`.

Fields:

- `data.artist`: `id` and `name`, both present when not null, and
  `thumbnail_url` (nullable): 544 x 544, smart crop; see [Image size](conventions.md#image-size).
- Each element of `data.songs`: `track_id`, `title`, `artists`, `album`,
  `album_id`, `duration_seconds`, `thumbnail_url` (544 x 544, smart
  crop; see [Image size](conventions.md#image-size)). All of them are
  always present here, `album` and `album_id` included, unlike
  `/artist` and `/tracks`, where they can be `null`: a song the
  provider lists without an album is omitted, see Malformed results.
- Each element of `data.albums`: `id` (the provider's `browseId`),
  `title`, `artists`, `year` (nullable), `thumbnail_url` (nullable;
  544 x 544, smart crop; see [Image size](conventions.md#image-size)).
  Search results do not carry a playlist id: to play an album, open it
  with `GET /album/{id}` and use its `audio_playlist_id`.
- `artists`, on both `songs` and `albums`, is a list of `{id, name}`,
  but `id` inside it is nullable: the external provider can mention an
  artist without a link, and that element arrives with `name` but
  `id: null`. An artist with a null id never matches the primary
  artist, no matter what, so it always falls into the second group
  described above — it is never dropped. `artists` is `[]`, never
  `null`, when the external provider has no artist data for that
  result (a compilation or a soundtrack); a result with `artists: []`
  also never matches the primary artist, so it falls into the second
  group too, without being dropped.

Malformed results: a result the provider returns without a field this
contract requires (song: `track_id`, `title`, `album`, `album_id`,
`duration_seconds`, `thumbnail_url`; album: `id`, `title`; artist: `id`,
`name`), or with any field of the wrong type (including `year` and
`thumbnail_url`, which can be `null` but not another type), is omitted
and recorded in the server log; the rest is returned with 200 `ok:
true`. A song without an album is one of these cases. For the artist,
`data.artist` is `null` and no reordering happens. Exception: if the
provider returned songs and none could be read, or returned albums and
none could be read, the whole response is 502 `upstream_error` and is
not cached, because it points to a format change at the provider and not
to an empty search; this is evaluated separately for songs and for
albums, and the artist is outside this rule. A provider answer with no
songs or no albums is the normal empty result (200, cached), not this
case. A provider response that is not a list of results is also 502
`upstream_error`. An omitted result stays omitted while the response is
cached (1 hour at most).
