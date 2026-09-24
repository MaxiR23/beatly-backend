# Public share endpoints

These five endpoints back share links: a preview card for an album, an
artist, a playlist, a genre playlist or a standalone track, opened by
someone who may have no session and no app installed. **They are served
with no token** — none of the five declares
`Depends(get_current_user_id)`. This is not the first router in the API
to work this way: the four endpoints of `docs/api/genres.md`
(`GET /genres`, `GET /genres/{slug}/playlists`,
`GET /genres/{slug}/categories`, `GET /genre-playlists/{playlist_id}/tracks`)
already serve curated content with no token. What is new here is
`GET /public/playlists/{playlist_id}`, which can serve a playlist that
belongs to a specific user — the first endpoint in this API to expose a
user's own data with no session behind it.

None of the five tables below has a 401 row: there is no token to be
missing or invalid.

## GET /public/album/{album_id}

A public projection of `GET /album/{album_id}` — see `docs/api/album.md`
for the full mapping. Reuses `services/album_service.py::get_album()` as
is, so it inherits that endpoint's behavior exactly: `data.tracks` comes
from the album's audio playlist, never from the album payload's own track
list, and a well-formed but nonexistent `album_id` is **502, never 404**
(`docs/adr/002-nonexistent-album-id-maps-to-upstream-error.md`).

| Case | Status | Body |
|---|---|---|
| Album found | 200 | `ok: true`, `data` with the album and its tracks |
| Album found, `audio_playlist_id` is `null` upstream | 200 | `ok: true`, `data.tracks: []`, the rest of the album populated as usual |
| `album_id` does not start with the required prefix | 422 | `ok: false`, `reason: "invalid_request"` |
| `album_id` is well formed but no album matches it | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider failed, including a response whose layout could not be parsed | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`data` is the same fields as `GET /album/{album_id}` **minus**
`audio_playlist_id`, `other_versions` and `related_recommendations`: a
share card shows the album itself, not the internal id used to fetch its
tracks or its browse carousels. `data`: `id`, `title`, `year` (nullable),
`artists`, `track_count` (nullable), `duration_seconds`, `thumbnail_url`
(nullable), `tracks`. See `docs/api/album.md` for the shape of each
track.

A successful response may be served from a server-side Redis cache and
be up to **24 hours** stale relative to the external provider: this
endpoint shares its cache entry with `GET /album/{album_id}` (same
provider operation, same key, same TTL), not a cache of its own. The
cache never changes the response's shape, status or reason, and a Redis
failure is invisible to the client. See
`docs/adr/005-provider-cache-lives-in-the-services.md` for why.

## GET /public/artist/{artist_id}

A public projection of `GET /artist/{artist_id}` — see
`docs/api/artists.md` for the full mapping. Reuses
`services/artist_service.py::get_artist()` as is: a well-formed but
nonexistent `artist_id` is **502, never 404**, for the same reason as
album.

| Case | Status | Body |
|---|---|---|
| Artist found | 200 | `ok: true`, `data` with the artist and its three lists |
| `data.songs` empty because the provider has no top-songs section | 200 | `ok: true`, `data.songs: []` |
| `data.albums` empty because the provider has no albums section | 200 | `ok: true`, `data.albums: []` |
| `data.singles` empty because the provider has no singles/EPs section | 200 | `ok: true`, `data.singles: []` |
| `artist_id` does not match the required pattern | 422 | `ok: false`, `reason: "invalid_request"` |
| `artist_id` is well formed but no artist matches it | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider failed, including a response whose layout could not be parsed | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`data` is the same fields as `GET /artist/{artist_id}` **minus**
`related`: a share card does not need a jumping-off point to other
artists. `data`: `id`, `name`, `thumbnail_url` (nullable), `songs`,
`albums`, `singles`.

`data.singles` travels **whole**, exactly as the provider sends it,
mixing `Single`, `EP` and items with `type: null` in the provider's own
order, each carrying its own `type` so a client can group them if it ever
wants to. This is a deliberate decision by the repo owner: the app's own
authenticated artist endpoint already returns `singles` unfiltered, and
filtering here would create a difference between the two with no reason
behind it.

A successful response may be served from a server-side Redis cache and
be up to **12 hours** stale relative to the external provider: this
endpoint shares its cache entry with `GET /artist/{artist_id}` (same
provider operation, same key, same TTL), not a cache of its own. The
cache never changes the response's shape, status or reason, and a Redis
failure is invisible to the client. See
`docs/adr/005-provider-cache-lives-in-the-services.md` for why.

## GET /public/playlists/{playlist_id}

Returns a user's playlist with its tracks, **only if it is public**. A
playlist is public when its owner set `is_public: true` via
`POST /playlists` or `PATCH /playlists/{id}`.

| Case | Status | Body |
|---|---|---|
| Playlist found and public | 200 | `ok: true`, `data` |
| Playlist has no tracks | 200 | `ok: true`, `data.tracks: []`, `data.total_duration_seconds: 0`, `data.thumbnails: []` |
| Playlist does not exist, or exists but is not public | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Malformed `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**The response body of a private playlist and of one that does not exist
is deliberately identical** — same status, same `reason`, no field that
tells them apart. This is the one thing this endpoint has to get right:
serving playlist data with no token behind it means the `is_public`
filter is the only thing standing between a private playlist and a
stranger who knows its id. The query that reads the playlist filters
explicitly by `.eq("is_public", True)`; it does not rely on Supabase RLS,
because this backend's client is service-role and bypasses RLS entirely.

`data`: `id`, `title`, `description` (nullable), `owner`, `track_count`,
`has_more`, `total_duration_seconds`, `tracks`, `thumbnails`. There is no
`thumbnail_url` field on this DTO — see the note below.

`owner` is `{"type": "user"}` for this endpoint, always. `owner_id` is
**never** returned, on any playlist: profiles are deliberately not
readable by third parties, so an owner id must never resolve to a
person. If a client ever needs to show more than a type, that is a new
field added to `owner` later (e.g. `owner.name`), not a lookup this
endpoint performs.

`track_count` is the playlist's real track count (not `len(tracks)`) and
`has_more` is `true` when the list was cut by the same 1000-track cap
`GET /playlists/{id}` uses — see `docs/api/playlists.md` for the details
of that cap. `total_duration_seconds` is calculated by the database over
every track in the playlist, not limited by the cap, same as
`GET /playlists/{id}`.

Each element of `data.tracks` has `track_id`, `title`, `artists`,
`album`, `album_id`, `duration_seconds`, `thumbnail_url` and `position`
— the same fields as `GET /playlists/{id}`, **minus `id`**: the internal
catalog uuid never crosses this boundary, in either direction (see
"Track identity" in `conventions.md`). `GET /playlists/{id}` and
`GET /playlists/liked` are the only two endpoints that keep exposing it,
for shape parity with an endpoint that predates that rule; this one, a
brand-new DTO, follows the rule as written.

`data.thumbnails` is an array of up to 4 miniatures for the share card's
mosaic, at most one per each of the first 4 tracks in playlist order,
filled by a database RPC (`get_user_playlist_thumbnails`) rather than
derived in Python from `data.tracks`. It can come back `[]` — an empty
playlist, or one whose first tracks all lack a thumbnail — and that is a
normal 200, not an error. **4 is a cap, not a guarantee**: the RPC takes
the first 4 tracks in playlist order and only afterwards drops the ones
with no thumbnail, so a playlist with plenty of tracks can still return
fewer than 4 miniatures if the first few happen to lack one.

There is no `thumbnail_url` field here because `public.playlists` has no
cover-art column at all — the mosaic in `thumbnails` is this endpoint's
only cover. Compare with the genre playlist below, whose table does have
one.

## GET /public/genre-playlists/{playlist_id}

Returns one curated genre playlist with its tracks and metadata (title,
description, curated cover), combining what
`GET /genres/{slug}/playlists` lists and what
`GET /genre-playlists/{playlist_id}/tracks` returns into a single object
for a share card.

| Case | Status | Body |
|---|---|---|
| Playlist found | 200 | `ok: true`, `data` |
| Playlist has no tracks | 200 | `ok: true`, `data.tracks: []`, `data.total_duration_seconds: 0`, `data.thumbnails: []` |
| Playlist does not exist | 404 | `ok: false`, `reason: "playlist_not_found"` |
| Malformed `playlist_id` | 422 | `ok: false`, `reason: "invalid_request"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`data`: `id`, `title`, `description` (nullable), `owner`, `track_count`,
`has_more`, `total_duration_seconds`, `tracks`, `thumbnails`,
`thumbnail_url` (nullable). `owner` is `{"type": "app"}`, always.

`track_count` is the number of tracks actually returned (`len(tracks)`),
**not** the stored `genre_playlists.track_count` column: this endpoint
carries the list itself, so a stored counter that disagreed with it
would be an inconsistency visible in the same response. The full
collection is returned with no cap of its own — same caveat as
`docs/api/genres.md` documents for
`GET /genre-playlists/{playlist_id}/tracks`: the real ceiling is
PostgREST's project-wide `max-rows` (1000), past which a collection
would be silently truncated. `has_more` is always `false` today, by the
same construction. `total_duration_seconds` is **summed in Python** over
that same whole collection, not by a database RPC: doing this in Python
is exact here (unlike the reason a Python sum was avoided for likes)
because the read behind it has no cap to leave a track out of the sum.

`data.thumbnails` has the same shape and the same 4-item cap as the user
playlist's `thumbnails` above, filled by a different RPC
(`get_playlist_thumbnails`, reading `genre_playlist_tracks` instead of
`playlist_tracks`) — see `services/genre_service.py` for the mapping
between the two RPCs and the two tables they read; they must never be
swapped. As of `023_align_genre_playlist_thumbnail_filters.sql` the two
RPCs filter the same way: both drop a track whose `thumbnail_url` is
`NULL` **or** an empty string. Since `public.tracks.thumbnail_url` is
`NOT NULL`, the empty-string half is the one doing the work. So neither
`thumbnails` array can ever come back with an empty-string element.

`data.thumbnail_url`, unlike the user playlist DTO, **does** exist here:
it is the curated cover art a genre playlist can have
(`genre_playlists.thumbnail_url`), nullable. It is a separate thing from
`thumbnails`: `thumbnail_url` is the one hand-picked cover, `thumbnails`
is the auto-generated mosaic, and a client picks whichever it wants to
draw.

Each element of `data.tracks` has the same fields as
`GET /genre-playlists/{playlist_id}/tracks`: `track_id`, `title`,
`artists`, `album`, `album_id`, `duration_seconds`, `thumbnail_url` and
`position`. None of these fields can be null.

## GET /public/tracks/{track_id}

Returns a single track's metadata: title, artists with id, album, its
duration and its thumbnail, in one response, for a standalone share link
(not tied to a playlist or an album). The data comes from the same
provider method and call that `GET /tracks/{track_id}/lyrics` and
`GET /tracks/{track_id}/related` already make
(`get_watch_playlist(videoId=track_id, limit=1)`), reading the same
**first** item that `GET /tracks/{track_id}/upnext` already maps — the
track that was asked for, measured live against the provider before this
endpoint was written (28/28 tracks, 2026-09-14: see the correction in
`docs/api/tracks.md` for the `/upnext` note this measurement also
fixed).

| Case | Status | Body |
|---|---|---|
| Track found | 200 | `ok: true`, `data` |
| Track is a music video (no album) | 200 | `ok: true`, `data.album` and `data.album_id` `null` |
| No `track_id` matches on the external provider | 404 | `ok: false`, `reason: "track_not_found"` |
| The external provider failed, including a response whose layout could not be parsed, or whose first item does not match the requested `track_id` | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| `GET /public/tracks/` with no id | 404 | `ok: false`, `reason: "not_found"` — no route matches |

`data`: `track_id`, `title`, `artists`, `album` (nullable), `album_id`
(nullable), `duration_seconds` (nullable), `thumbnail_url` (nullable).

`album`/`album_id` always travel together and are `null` together. The
normal case for that is a music video (`videoType:
"MUSIC_VIDEO_TYPE_OMV"`, measured 3/3): the provider does not send an
album for one at all. This is **not** an error and **not** a 404.

`artists[].id` can be `null` when the provider mentions an artist with
no link, the same rule the rest of this API already follows.

**There is no 422 row**: unlike `/public/album/{id}` and
`/public/artist/{id}`, this endpoint does not validate the shape of
`track_id` with a `Path(pattern=...)`. Any string reaches the provider
and the probe decides the outcome — the same decision
`docs/api/tracks.md` documents for the four authenticated `/tracks/*`
endpoints, applied here without a new exception.

A well-formed `track_id` that the provider's watch playlist answers with
an **empty** track list is also `502 upstream_error`, never a 200 with
every field `null`: the provider already confirmed the track exists (the
404 probe did not fire), so an empty queue is an upstream anomaly, not a
documented empty state.

A successful response may be served from a server-side Redis cache and
be up to **24 hours** stale relative to the external provider: track
metadata (title, artists, duration, album) does not change the way a
playback queue does, so this endpoint carries the same TTL as
`/album`, `/lyrics` and `/credits`, not the shorter TTL
`/tracks/{track_id}/upnext` uses, even though both endpoints' data comes
from the same underlying watch-playlist call. This cache entry is its
own — it does **not** share a key with
`GET /tracks/{track_id}/upnext`, `/lyrics`, `/related` or `/credits`,
each of which is a distinct cached operation. The cache never changes
the response's shape, status or reason, and a Redis failure is invisible
to the client. See
`docs/adr/005-provider-cache-lives-in-the-services.md` for why.
