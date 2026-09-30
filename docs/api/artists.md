## GET /artist/{artist_id}

Looks up an artist on the external provider by id and returns their
name, top songs, albums, singles (including EPs) and related artists.
Authenticated: requires a Supabase JWT like the rest of the API.

| Case | Status | Body |
|---|---|---|
| Artist found | 200 | `ok: true`, `data` with the artist and its four lists |
| `data.songs` empty because the provider has no top-songs section for this artist | 200 | `ok: true`, `data.songs: []` |
| `data.albums` empty because the provider has no albums section for this artist | 200 | `ok: true`, `data.albums: []` |
| `data.singles` empty because the provider has no singles/EPs section for this artist | 200 | `ok: true`, `data.singles: []` |
| `data.related` empty because the provider has no related-artists section for this artist | 200 | `ok: true`, `data.related: []` |
| `artist_id` does not match the required pattern | 422 | `ok: false`, `reason: "invalid_request"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| `artist_id` is well formed but no artist matches it | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider failed, including a response whose layout could not be parsed | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| `GET /artist/` with no id | 404 | `ok: false`, `reason: "not_found"` — no route matches |

A successful response may be served from a server-side Redis cache and be
up to **12 hours** stale relative to the external provider. The cache
never changes the response's shape, status or reason, and a Redis
failure is invisible to the client: the endpoint responds with the same
body, status and reason as it would with no cache at all. See
`docs/adr/005-provider-cache-lives-in-the-services.md` for why.

The successful response carries `Cache-Control: max-age=<seconds left>`,
at most the operation's TTL (12 hours); see [Caching headers](conventions.md#caching-headers).

`artist_id` must match `^(MPLA)?UC`. **This pattern is an assumption we
make about the external provider's id format, not a rule the provider
itself enforces**: the library only ever does
`channelId.removeprefix("MPLA")` before using the id, with no format
validation of its own. `MPLA` is accepted as well as a bare `UC`
because the library explicitly strips that prefix, so it is a format
the provider resolves either way. If the provider ever starts emitting
a channel id with a different prefix, the symptom will be a 422 on an
artist that actually exists — not a crash, and not obviously this
pattern being stale — so this note is the first thing to check.

A well-formed but nonexistent `artist_id` responds **502, never 404**.
See `docs/adr/002-nonexistent-album-id-maps-to-upstream-error.md` for
the reasoning: the external provider's response for an unknown id and
for a layout it can no longer parse both surface as the same
underlying failure (a `KeyError` from the provider's own navigation
path), with nothing in the payload to tell them apart, so this
endpoint has no domain 404 either.

`data.id` is always the id that was requested, never the `channelId`
read back from the external provider's response: the provider's
`channelId` identifies a different channel (its video channel), not
the music channel that was requested.

`data.songs`, `data.albums`, `data.singles` and `data.related` are
returned whole, unpaginated, exactly as the external provider's single
response for this artist carries them, with the same reasoning
`docs/api/search.md` and `docs/api/album.md` document: the artist page
is the payload of this endpoint, not a growable collection in its own
right, so none of the four is wrapped in the `Paginated[T]` envelope.
The "view all" screen for the full albums/singles catalog (which would
require a second, paginated call to the provider) is out of scope.

An empty list in any of the four fields means the external provider
did not send that section for this artist — it is not an error being
swallowed. Any real failure to reach or parse the provider's response
is always 502 or 504, never a silently empty list.

`data.songs[]`: `album` and `album_id` always travel together — if
`album_id` is `null`, `album` is too. The external provider can send an
album name with no id; when that happens both fields come back `null`,
losing the name on purpose so the pair is always coherent instead of a
name with nothing for the client to link to.

`data.albums` and `data.singles` deliberately use different shapes.
An item in `data.singles` has no `artists` and no `audio_playlist_id`
because the external provider never sends those two fields for a
single or an EP — not because they are empty for that item. `type`
(distinguishing a `Single` from an `EP`) exists only on `data.singles`
items; `data.albums` items never carry it. That the two lists are not
interchangeable is intentional: the client renders them in separate
sections and does not need them to share a shape.

`description`, `videos`, `subscribers`, `views` and `monthlyListeners`
are deliberately not exposed, on the artist or on any related artist —
same reasoning `docs/api/album.md` documents for `description`: it is
third-party editorial content with its own attribution, and this API
does not carry it.

`data.thumbnail_url` is requested from the provider's CDN at 1200 x 1200
and each `data.related[].thumbnail_url` at 544 x 544, both with smart
crop: the first occurrence of `=w<digits>-h<digits>` (ASCII digits 0-9),
with an optional immediate `-p` (counted only as a whole flag, followed
by `-` or by the end of the URL), is replaced by `=w1200-h1200-p` or
`=w544-h544-p` respectively. `-p` is the smart crop; it is added if
missing and never duplicated, and a flag such as `-pd` is never taken for
`-p`, so `...=w2880-h1200-pd-l90-rj` becomes `...=w1200-h1200-p-pd-l90-rj`.
The rest of the
URL (for example `-l90-rj`) is left intact, with no host filter, and a
URL without that suffix is returned as is, never as an error. Example:
`...=w2880-h1200-p-l90-rj` becomes `...=w1200-h1200-p-l90-rj` on the
artist, and `...=w226-h226-p-l90-rj` becomes `...=w544-h544-p-l90-rj` on
a related artist.
`data.songs[]`, `data.albums[]` and `data.singles[]` return the URL as the
provider sends it, unrewritten. 544 x 544 is the largest variant the
provider sends today for related artists, so no more is requested.
`GET /public/artist/{artist_id}` inherits the same `thumbnail_url` (see
`docs/api/public.md`). A response cached before this change may keep the
previous URL until it expires (12 hours).

Fields:

- `data`: `id`, `name`, `thumbnail_url` (nullable; 1200 x 1200, smart
  crop, see above), `songs`, `albums`, `singles`, `related`.
- Each element of `data.songs`: `track_id` (nullable), `title`,
  `artists`, `album` (nullable), `album_id` (nullable),
  `duration_seconds` (nullable), `thumbnail_url` (nullable).
- Each element of `data.albums`: `id` (the provider's `browseId`),
  `title`, `artists`, `year` (nullable), `audio_playlist_id`
  (nullable), `thumbnail_url` (nullable) — same `AlbumRef` shape
  `docs/api/album.md` documents for `other_versions` and
  `related_recommendations`.
- Each element of `data.singles`: `id` (the provider's `browseId`),
  `title`, `year` (nullable), `type` (nullable, `"Single"` or `"EP"`),
  `thumbnail_url` (nullable).
- Each element of `data.related`: `id` (the provider's `browseId`),
  `name`, `thumbnail_url` (nullable; 544 x 544, smart crop, see above).
