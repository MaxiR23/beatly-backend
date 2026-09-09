## GET /tracks/{track_id}/upnext

Looks up the watch-next queue the external provider builds for a track
and returns up to 50 tracks. Authenticated: requires a Supabase JWT
like the rest of the API.

| Case | Status | Body |
|---|---|---|
| Track found | 200 | `ok: true`, `data.tracks` with up to 50 tracks |
| Track found, provider queue has no playable entries | 200 | `ok: true`, `data.tracks: []` |
| No `track_id` matches on the external provider | 404 | `ok: false`, `reason: "track_not_found"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| The external provider failed, including a response whose layout could not be parsed | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| `GET /tracks//upnext` with no id | 404 | `ok: false`, `reason: "not_found"` — no route matches |

The **first** element of `data.tracks` is the requested track itself
(the queue always starts with what is currently playing). It is
returned as-is, and the provider sends it with no `artists` and no
`album`: `artists: []` and `album`/`album_id` are `null` on that item,
not an error.

## GET /tracks/{track_id}/lyrics

Looks up the lyrics of a track. Authenticated: requires a Supabase JWT
like the rest of the API.

| Case | Status | Body |
|---|---|---|
| Track found, lyrics available | 200 | `ok: true`, `data.lyrics` populated |
| Track found, provider has no lyrics tab for it | 200 | `ok: true`, `data.lyrics: null` |
| Track found, provider has a lyrics tab but returns no content | 200 | `ok: true`, `data.lyrics: null` |
| No `track_id` matches on the external provider | 404 | `ok: false`, `reason: "track_not_found"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| The external provider failed, including a response whose layout could not be parsed | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| `GET /tracks//lyrics` with no id | 404 | `ok: false`, `reason: "not_found"` — no route matches |

`data.lyrics: null` is **never** a 404: it is the expected shape for a
track that has no lyrics on the provider (most commonly an
instrumental). The 404 `track_not_found` above is reserved for a
`track_id` that does not exist at all.

`data.lyrics` shape, when not `null`:

- `has_timestamps`: the only field the client needs to branch on.
- `source`: nullable, the provider's attribution string.
- `lines`: a list of `{text, start_ms, end_ms}`. When `has_timestamps`
  is `false`, `start_ms` and `end_ms` are `null` **explicitly on every
  line, never absent**. When `has_timestamps` is `true`, both are
  present and in milliseconds.

## GET /tracks/{track_id}/related

Looks up content related to a track and returns it split into songs,
artists and albums. Authenticated: requires a Supabase JWT like the
rest of the API.

| Case | Status | Body |
|---|---|---|
| Track found, related content available | 200 | `ok: true`, `data` with `songs`, `artists`, `albums` |
| Track found, provider has no related tab for it | 200 | `ok: true`, all three lists `[]` |
| Track found, related tab has no item that survives the song filter | 200 | `ok: true`, `data.songs: []`, the other two populated as usual |
| No `track_id` matches on the external provider | 404 | `ok: false`, `reason: "track_not_found"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| The external provider failed, including a response whose layout could not be parsed | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| `GET /tracks//related` with no id | 404 | `ok: false`, `reason: "not_found"` — no route matches |

Only items whose `videoType` is `MUSIC_VIDEO_TYPE_ATV` enter
`data.songs`; this is the same rule already applied everywhere else in
this system. Sections are identified by the **shape of their items**
(the discriminating key — `videoId`, `audioPlaylistId` or
`subscribers` — tested by presence, not by truth), **never by title**
(the provider localizes it) **and never by order** (the provider
changes it without notice). Recommended playlists, the artist's
biography section and any non-ATV item are excluded from the
response.

`videoType` is only ever set by the branch of the provider's parser
that produces the shape this endpoint reads (`parse_song_flat`); a
song item that reached the response through the other song-parsing
branch would carry no `videoType` and would not enter `data.songs`.
This is conservative and correct — only confirmed ATV items enter —
but worth knowing if `data.songs` ever looks shorter than expected.

`data.songs` **can include alternate official versions of the same
track** (remasters, live versions, official instrumentals): these are
not duplicates, they enter because they are ATV, which is the explicit
filter this endpoint applies.

## Common to all three

See `docs/adr/003-nonexistent-track-id-maps-to-track-not-found.md` for
why a nonexistent `track_id` on `/tracks/*` responds 404, unlike
`/album/{album_id}` and `/artist/{artist_id}`
(`docs/adr/002-nonexistent-album-id-maps-to-upstream-error.md`): here
the provider exposes the distinction in a separate, structured field
(`get_song(track_id).playabilityStatus.status`), which those two
domains do not have an equivalent of.

A track that exists but cannot currently be played (`UNPLAYABLE`: a
livestream no longer available, a video still processing) responds
**200 normally**, not 404. A reason of its own for "exists but is not
playable" belongs to the audio/streaming domain and does not exist
today.

None of `data.tracks`, `data.lyrics.lines`, `data.songs`,
`data.artists` or `data.albums` is wrapped in the `Paginated[T]`
envelope, with the same reasoning `docs/api/search.md`,
`docs/api/album.md` and `docs/api/artists.md` document: each is the
whole payload the provider's single response carries for this track,
not a growable collection of our own, and there is no cursor to emit
over it.

An empty list or a `lyrics: null` means the provider has nothing of
that kind for this track — it is never an error being swallowed. Any
real failure to reach or parse the provider's response is always
502 or 504.

`data.tracks[].duration_seconds` (on `/upnext`) and
`data.songs[].duration_seconds` (on `/related`) are both nullable. On
`/upnext` the value comes from the provider's numeric field when
present, and otherwise from parsing its text duration (`"3:07"`); a
value that cannot be parsed returns `null` rather than an error.

`/lyrics` and `/related` each make **two** chained calls to the
provider by design: the browse id for the second call comes out of the
first (the watch playlist). None of the three routes makes a call per
item.

Fields:

- `/upnext` `data`: `tracks`, each element `track_id`, `title`,
  `artists`, `album` (nullable), `album_id` (nullable),
  `duration_seconds` (nullable), `thumbnail_url` (nullable).
- `/lyrics` `data`: `lyrics` (nullable), an object with
  `has_timestamps`, `source` (nullable), `lines` (each element `text`,
  `start_ms` nullable, `end_ms` nullable).
- `/related` `data`: `songs` (same shape as `/upnext` tracks),
  `artists` (each element `id`, `name`, `thumbnail_url` nullable),
  `albums` (each element `id`, `title`, `artists`, `year` nullable,
  `audio_playlist_id` nullable, `thumbnail_url` nullable — the same
  `AlbumRef` shape `docs/api/album.md` documents).
