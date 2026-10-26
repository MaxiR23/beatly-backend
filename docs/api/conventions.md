# API conventions

Rules that apply to every endpoint. Per-endpoint documents only
describe what differs from this.

Field-level schemas are in OpenAPI: http://localhost:8000/docs

## Response shape

    {"ok": true, "data": ...}
    {"ok": false, "reason": "..."}

`ok` is always present. `reason` is a stable snake_case identifier
meant to be read by the client, never a message to display.

## Status codes

| Case | Status | reason |
|---|---|---|
| Success | 200 | none |
| Expected empty state | 200 | specific to the case |
| Parent resource does not exist | 404 | `*_not_found` |
| Invalid input | 422 | `invalid_request` |
| Conflict with current state | 409 | specific to the case |
| Not authenticated | 401 | `unauthorized` |
| No permission | 403 | `forbidden` |
| Too many requests | 429 | `rate_limited` |
| Upstream service failed | 502 | `upstream_error` |
| Upstream timed out | 504 | `upstream_timeout` |
| Unhandled internal error | 500 | `internal_error` |

## The distinction that matters

A 200 with `ok: false` is not an error. The request succeeded and the
answer is that there is nothing. No endpoint returns this shape today:
every list that could is paginated, and a paginated one answers "nothing
here" with an empty first page instead, as the Pagination section
describes.

A 404 means the parent resource does not exist. A slug that matches no
genre returns 404.

Clients check `ok` in the body, not the status code, to tell "nothing
here" from "navigation error".

## Reasons

| reason | Status | Meaning |
|---|---|---|
| `invalid_request` | 422 | Missing or malformed parameters |
| `unauthorized` | 401 | Missing or invalid token |
| `forbidden` | 403 | Authenticated but not allowed |
| `upstream_error` | 502 | A provider or the database failed |
| `upstream_timeout` | 504 | A provider did not respond in time |
| `internal_error` | 500 | Unhandled error. If you see this, it is a bug |
| `profile_not_found` | 404 | Authenticated user has no profile row |
| `library_item_not_found` | 404 | No library item matches user_id, kind, external_id |
| `username_taken` | 409 | Requested username already belongs to another profile |
| `report_not_found` | 404 | No bug report matches the given id |
| `playlist_not_found` | 404 | No playlist matches the given id. On `/playlists/{id}` it also covers a playlist the caller cannot edit, which is deliberately indistinguishable from one that does not exist. On the `/public/...` endpoints it also covers a playlist that exists but is not public, deliberately indistinguishable from one that does not exist. Also returned by `GET /genre-playlists/{playlist_id}/tracks`, where the id is that of a curated genre playlist |
| `track_already_in_playlist` | 409 | `POST /playlists/{id}/tracks` was given a track the playlist already contains. The bulk endpoint skips such tracks instead of returning this |
| `order_key_conflict` | 409 | A write that places a track (`POST /playlists/{id}/tracks`, `.../tracks/bulk`, `.../move-track`) kept colliding with concurrent writes to the same playlist and gave up after 3 attempts. Nothing was written; the request can be retried |
| `invalid_cursor` | 422 | The cursor is malformed or no longer valid |
| `track_not_found` | 404 | No track on the external provider matches the given id, as reported by its own playability status. Also returned when the requested track does not meet the [track artists rule](#track-artists) (`GET /tracks/{id}/upnext`, `GET /public/tracks/{id}`) |
| `genre_not_found` | 404 | No genre matches the given slug. Returned by `GET /genres/{slug}/playlists` and `GET /genres/{slug}/categories` |
| `rate_limited` | 429 | Too many requests from this user in a short window. Returned by `POST /errors/playback` past its per-user cap; nothing was stored |

## Track identity

A track has two identities: the internal catalog uuid (`tracks.id`,
Postgres-generated) and the external provider id (`tracks.track_id` —
the id the client plays and references, whatever the upstream provider
is).

**The API speaks the provider id only.** The internal uuid never
crosses an endpoint boundary, in either direction: not in request
params or bodies, not in response payloads. Where a table stores the
internal uuid (`playlist_tracks`), the provider->uuid resolution
happens inside the service or the RPC
(`get_owned_playlists_with_track`, `remove_playlist_track`), invisible
to the client.

Rationale: the client only knows provider ids; leaking the internal
uuid creates two ways to reference the same track and forces every new
domain to re-decide which one to accept. The provider is an
implementation detail: if it ever changes, `tracks.track_id` keeps
being "the external id", and neither the rule nor any endpoint
contract moves.

## Track artists

Every song the API returns or accepts carries `artists`, a list of
`{"id": string | null, "name": string}`. The rule:

1. The list is never empty.
2. The first artist has an `id`. An empty string counts as missing: it
   links to nothing.
3. Any other artist may have `id: null` (an artist the provider names
   without a link). It is always serialized as an explicit `"id": null`,
   never with the key omitted.

"First" is the order received: from the provider, from the request body,
or as stored. The rule is one type in the code
(`models/artist_refs.py`); the database backs rule 2 with the CHECK
`tracks_artists_primary_has_id` on `tracks` (migration `041`, created
`NOT VALID`: rows loaded before it are not checked).

What each kind of endpoint does with a song that breaks it:

| Where the song comes from | What happens |
|---|---|
| A list built from the provider (`/search` songs, `/album/{id}` tracks, `/artist/{id}` songs, `/tracks/{id}/upnext` after the first item, `/tracks/{id}/related` songs, and the `/public/album` and `/public/artist` equivalents) | The song is left out of the list and a warning is logged with the kind of list and the reason. If every song is left out the list is `[]` in a 200, an expected empty state |
| A single requested song: `GET /public/tracks/{id}`, and the first item of `GET /tracks/{id}/upnext` (the requested track) | 404 `track_not_found` |
| A write: `POST /likes`, `POST /playlists/{id}/tracks`, `POST /playlists/{id}/tracks/bulk`, `POST /plays` | 422 `invalid_request`, nothing is written. In the bulk, one song that breaks it rejects the whole batch |
| A read from the database (likes, playlists, genre playlists, public playlists) | 502 `upstream_error` if a stored row breaks it |

Album artists (`artists` of an album, or of an album reference) do not
follow this rule: `id` is optional and the list may be empty.

## Pagination

Every endpoint that returns a list that can grow uses cursor
pagination. Query params: `limit` (default 50, max 100, enforced
server-side) and `cursor` (opaque — the client stores and echoes it,
never inspects or builds it).

Paginated responses wrap the list:

    {"ok": true, "data": {"items": [...], "page": {
        "limit": 50, "next_cursor": "..." , "has_more": true,
        "total": 370}}}

`total` is exact and present only on the first page (request without
cursor); `null` on subsequent pages — the client keeps it. End of the
collection is `has_more: false` with `next_cursor: null`. An invalid
or expired cursor is 422 `invalid_cursor`.

Under the hood this is keyset pagination: each domain declares a sort
key (plus id as tiebreaker) and the shared helper in
`core/pagination.py` does the rest. Offset pagination is not used
anywhere: its cost grows with the offset and rows shift between pages.

Keyset pagination is exact while no write changes a row's sort key
during a walk. Where a write can — a reorder that rewrites the key — a
row moved from ahead of the cursor to behind it is skipped for the rest
of that walk, and one moved from behind the cursor to ahead of it is
returned a second time. This is accepted, not a bug, for any endpoint
whose sort key a write can change; its own docs say which writes those
are. An endpoint whose sort key no write changes does not have this
limitation.

A cursor belongs to the ordering it was emitted under. An endpoint that
accepts more than one ordering rejects a cursor emitted under a
different one with 422 `invalid_cursor`, rather than returning a page
sorted the wrong way — so when the client changes the ordering, it
discards the cursor and requests the first page again. An endpoint with a
filter ties the cursor to the filter's value too: one emitted under another
filter, or without one, is 422 `invalid_cursor`.

An endpoint can return, besides `items` and `page`, a value fixed on the
first page of a read that every cursor of that read carries, so each page
returns it again without a new lookup. Today only the likes endpoints do
(`checkpoint`, see `likes.md`). For such an endpoint, a cursor emitted
before it carried the value is 422 `invalid_cursor`, like any invalid or
expired cursor. No other endpoint changes, and there is no new reason.

For a paginated endpoint, the expected-empty state is the first page
coming back empty — `ok: true`, `items: []`, `has_more: false`,
`total: 0` — not an `ok: false` with an empty-state reason as described
in "The distinction that matters". That section covers non-paginated
list endpoints; a paginated one only ever has one shape for "nothing
here", the same one it uses for the end of a longer collection.

## Caching headers

Every response carries a `Cache-Control` header, so a client needs no
cache times of its own: the backend is the only source of them. There are
four categories, by domain and not by HTTP method.

| Endpoints | Header on success |
|---|---|
| Served from the server-side cache: `/search`, `/album/{id}`, `/artist/{id}`, the other three `/tracks/{id}/...` endpoints, and `/public/album/{id}`, `/public/artist/{id}`, `/public/tracks/{id}` | `max-age=<N>` |
| `/tracks/{id}/upnext`, served from the server-side cache but filtered per caller | `private, max-age=<N>` |
| User data: `/library`, `/likes`, `/playlists`, `/profile`, `/bug-reports`, `/plays`, `/recents`, for every method | `private, no-cache` |
| Everything else (`/genres`, `/genre-playlists`, `/errors`, `/public/playlists/{id}`, `/public/genre-playlists/{id}`, `/health`) | `no-store` |

`N` is the time the server-side cache entry has left, in whole seconds,
not the operation's full TTL. A client that keeps the response for `N`
seconds never holds it longer than the server would still serve it. When
the response was just fetched from the external provider (a cache miss),
`N` is the full TTL, whether or not the entry could be written. The
header carries neither `private` nor `public`: the endpoints that require
a token are already not stored by shared caches, and on the public share
endpoints an intermediary storing the response is desirable. The one
exception is `/tracks/{id}/upnext`, whose response depends on the caller's
history: it carries `private, max-age=<N>`, and not `no-cache`, because the
front pins the suggestions per source and with `no-cache` it could ask for
them again in the middle of a source and the list would change (front #84).
If the
server cannot determine how much time the entry has left, the response
goes out as `no-store` instead.

Every error response, on any endpoint, carries `no-store`, whatever the
success header of that endpoint is. The body, status and reason of a
response never depend on these headers. `Vary: Accept-Encoding` (see
Compression) can accompany any of these values. It does not change the
`max-age` or the value of `Cache-Control`, but a shared cache keeps one
copy of the response per encoding.

## Compression

The server compresses a response with gzip when the request carries
`gzip` in `Accept-Encoding` and the body is at least `GZIP_MINIMUM_SIZE`
bytes (1024 by default). The response then carries `Content-Encoding:
gzip`, a `Content-Length` of the compressed body, and decompresses to
exactly the same JSON. Below the threshold the response goes out
uncompressed and without `Vary`. Every response of at least the threshold
carries `Vary: Accept-Encoding`, whether or not the request asked for
gzip.

Status, body, reasons and `Cache-Control` do not change, errors included:
a large error is compressed like any other body. The 500 `internal_error`
is never compressed. An HTTP client that sends `Accept-Encoding: gzip` and
decompresses by itself, which is the normal case on Android and iOS, has
nothing to do.

Why gzip sits inside the cache-control middleware, and the numbers behind
the threshold and the level: `docs/adr/020-gzip-sits-inside-default-cache-control.md`.

The threshold is 1024 bytes because under about 1 KB a response travels in
one TCP segment with or without gzip, so compressing saves no round trip
and only spends CPU. The level is 6 (`GZIP_COMPRESSLEVEL`, 1-9): level 9
shrinks under 1% more for about 20% more CPU.

## Image size

Every `thumbnail_url`, and every element of `thumbnail_urls` and
`thumbnails`, of a track, an album, a playlist or an artist in a list is
requested from the provider's CDN at 544 x 544 with smart crop, so a
client never gets a 60 x 60 cover to show in a list.

The rule: the first occurrence of `=w<digits>-h<digits>` (ASCII digits
0-9), with an optional immediate `-p` (counted only as a whole flag,
followed by `-` or by the end of the URL), is replaced by
`=w544-h544-p`. `-p` is the smart crop; it is added if missing and never
duplicated. The rest of the URL (for example `-l90-rj`) is left intact,
with no host filter. Examples: `...=w60-h60-l90-rj` becomes
`...=w544-h544-p-l90-rj`, and `...=w226-h226-p-l90-rj` becomes
`...=w544-h544-p-l90-rj`. A larger URL also goes down to 544.

A URL without that suffix (for example one from `i.ytimg.com`) is
returned as is and is never an error; `null` stays `null`.

The URLs a client stores (likes, the tracks of a playlist, the library)
are rewritten when they are read: the database keeps what was sent, and
the responses of `POST /likes`, `POST /playlists/{playlist_id}/tracks` and
`POST /library` already come back rewritten.

The rewrite happens on read, in Python, and not when writing or in a
migration. Rewriting on write would leave the rows already stored small.
A migration plus a backfill would copy the rule into SQL (as `036`
already does for recents), would have to be applied by hand, and would
skip the shared helper. Reading covers old and new rows and stores
nothing new.

Exceptions: the artist image of `GET /artist/{artist_id}` is 1200 x 1200
(see `artists.md`), and `GET /recents` and `POST /recents` go to
512 x 512 without smart crop (see `activity.md`).

A response cached on the server before this rule may carry the previous
URL until it expires (the TTL of each endpoint is in its own document).
That holds for `thumbnail_url` and not for the tiers below, which are
computed when the response is sent.

### Image tiers

Next to every `thumbnail_url` that follows the rule above, the response
carries `image`: the same picture in three square sizes, all with smart
crop.

    "thumbnail_url": "https://.../abc=w544-h544-p-l90-rj",
    "image": {
      "small":  "https://.../abc=w192-h192-p-l90-rj",
      "medium": "https://.../abc=w600-h600-p-l90-rj",
      "large":  "https://.../abc=w1320-h1320-p-l90-rj",
      "color":  "#3a5f9c"
    }

| Tier | Size | For |
|---|---|---|
| `small` | 192 | rows and the mini player |
| `medium` | 600 | carousels, grids and tiles |
| `large` | 1320 | the player and heroes |

Next to every `thumbnail_urls` (library, playlists, genre playlists) and
every `thumbnails` (public playlists) the response carries `mosaic`: a list
with the same length and the same order, each element being `small`,
`medium` and `large` as above, and `color`. `[]` gives `[]`.

- The three tiers and `color` are always there. `image` is `null` when `thumbnail_url`
  is `null` or `""`; the key is always present.
- They are derived from the `thumbnail_url` that is already rewritten, in the
  response, with the same rule as above and the same first-occurrence,
  no-host-filter behaviour. A URL without the size suffix is repeated as is in
  the three tiers and is never an error.
- The legacy fields do not change: `thumbnail_url` and `thumbnail_urls` stay at
  544, the artist at 1200 (so `image.large` of an artist is 1320 while its
  `thumbnail_url` is 1200), and recents at 512 without smart crop. A new
  client should read `image` and `mosaic`.
- The tiers are never stored; `color` is (see below). A response cached on the
  server before the tiers existed still carries them, because they are
  computed on the way out, and so does its color.
- `POST /plays` (its `metadata` is stored as sent) and the `avatar_url` of
  `/profile/me` carry no tiers. No request accepts `image` or `mosaic`; sending
  `metadata.image` to `POST /recents` is `422 invalid_request`.
- The responses grow: each `image` adds three URLs, and each `mosaic` three per
  element.

#### Color

`color` is the dominant color of the picture, as `"#rrggbb"` in lowercase, or
`null`. It is in `image`, in every element of `mosaic` and in
`metadata.image` of a recent.

- It is `null` until it has been computed. The first response that carries an
  image the server has not seen before has `color: null`; the color is
  computed after that response is sent and appears in a later one. A response
  a client holds under `Cache-Control: max-age` can keep `null` until it
  expires.
- It is never an error and never has a `reason`. If the colors cannot be read
  (the database failing, for example) the response keeps its status and its
  body and every `color` is `null`; nothing in the status code tells it.
  Reading the stored image colors never fails the request.
- It is one color per picture: every size of one picture (`small`, `medium`,
  `large`, and the `544` of `thumbnail_url`) has the same color.
- IMPORTANT: `IMAGE_COLOR_ALLOWED_HOSTS` must be set for colors to appear.
  The list is empty by default: nothing is downloaded and every `color` is
  `null`.
- It is only computed for pictures on the provider's CDN hosts the server
  allows. A picture on another host, or an `image` of `null`, has no color.
  A picture that could not be downloaded or decoded stays `null` and is
  tried again at most once every 24 hours.
- A client that finds `null` should show its neutral background.
- Requests never accept `color`.

##### Adjustment to the dark theme

`color` is not the raw dominant color: it is adjusted so it works as a tint
over the app's dark background (`#0e0e0e`) under white text. Only the
lightness changes; the hue is kept (up to 8-bit rounding).

- A color too dark to stand out from the background is lightened until its
  WCAG contrast against it reaches the minimum (default 1.5). Black becomes
  `#323232`.
- A color too light for white text is darkened until the contrast of white on
  top reaches the minimum (default 4.5, the WCAG AA text level). White
  becomes `#767676`.
- A color that meets both is returned as is.

| Environment variable | Default | Meaning |
|---|---|---|
| `IMAGE_COLOR_BACKGROUND` | `#0e0e0e` | background of the app, `#rrggbb` lowercase |
| `IMAGE_COLOR_MIN_BACKGROUND_CONTRAST` | `1.5` | minimum contrast against the background, 1-21 |
| `IMAGE_COLOR_MIN_WHITE_TEXT_CONTRAST` | `4.5` | minimum contrast of white text on the color, 1-21 |

The server refuses to start if no color can meet both minimums at once. The
reasoning is in `docs/adr/021-image-colors-are-read-per-request-and-computed-after-the-response.md`.
