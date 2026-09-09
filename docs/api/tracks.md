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

## GET /tracks/{track_id}/credits

Looks up who performed, wrote, produced and provided the music metadata
for a track. Authenticated: requires a Supabase JWT like the rest of
the API.

| Case | Status | Body |
|---|---|---|
| Track found, credits available | 200 | `ok: true`, `data` with the typed sections the provider sent plus `other_sections` |
| Track found, provider has no credits for it, or the credits page layout could not be navigated on a track the ADR 003 probe confirms exists (the two are indistinguishable by design, see ADR 004) | 200 | `ok: true`, `data.performed_by`, `data.written_by`, `data.produced_by`, `data.music_metadata_provided_by` all `null`, `data.other_sections: []` |
| No `track_id` matches on the external provider | 404 | `ok: false`, `reason: "track_not_found"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| The external provider returned an HTTP error, or a 200 body that could not be parsed at all (`ValueError`) | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| `GET /tracks//credits` with no id | 404 | `ok: false`, `reason: "not_found"` — no route matches |

Unlike `/upnext`, `/lyrics` and `/related`, `/credits` does not go
through `get_watch_playlist`: it builds the provider's browse id
directly as the literal prefix `MPTC` followed by `track_id`, with no
call to `get_album` and no query against our own database. This only
resolves to the right page when `track_id` is the id of the **audio
track**, not the id of a music video of the same song, and the
provider's own validation of the browse id does not enforce that: it
only checks for the `MPTC` prefix, which `"MPTC" + track_id` always
has regardless of what `track_id` actually points to.

Not every source of `track_id` in this system guarantees the audio-id
condition. `search(filter="songs")` (measured 20/20 ATV) and
`data.songs` from `/related` (filtered explicitly on
`videoType == "MUSIC_VIDEO_TYPE_ATV"`, see above) do. `data.tracks`
from `/upnext`, `data.songs` from `/artist/{artist_id}` and
`data.tracks[].track_id` from `/album/{album_id}` apply no `videoType`
filter at all — for `/album` this has been measured live against the
provider: the `track_id` it exposes for a song can be that song's
music-video id, not its audio id. Calling `/credits` with a `track_id`
sourced from one of those three unfiltered places builds a browse id
that points at a different page: navigation fails and the response is
a 200 with the four typed sections `null` and `other_sections: []`,
exactly like a track that genuinely has no credits — never a 5xx and
never a 404. See
`docs/adr/004-missing-credits-dialog-is-an-expected-empty.md` for the
full limit.

A `data` with the four typed sections `null` and `other_sections: []`
is a 200, **never** a 404 and never a 502: it means the provider did
not deliver a navigable credits dialog for this track. See
`docs/adr/004-missing-credits-dialog-is-an-expected-empty.md`: this
shape does not distinguish a track that genuinely has no credits from
one whose credits page layout could not be navigated, the same
"expected empty, not a swallowed failure" rule the rest of this file
applies.
`localized_title` comes localized by the provider and is for display
only, never for the client to branch on. A section the provider sends
that is not one of the four recognized ones is not discarded: it lands
in `data.other_sections`. `/credits` makes a **single** call to the
provider on the happy path — unlike `/lyrics` and `/related`, which
each make two — and only makes a second call, to the same probe ADR
003 already uses, on the failure/empty path. See
`docs/adr/004-missing-credits-dialog-is-an-expected-empty.md` for why
a navigation failure on this endpoint becomes a 200 with empty credits
instead of always being a 502.

## Common to all four

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
`data.artists`, `data.albums` or `data.other_sections` is wrapped in
the `Paginated[T]` envelope, with the same reasoning
`docs/api/search.md`, `docs/api/album.md` and `docs/api/artists.md`
document: each is the whole payload the provider's single response
carries for this track, not a growable collection of our own, and
there is no cursor to emit over it.

An empty list or a `lyrics: null` means the provider has nothing of
that kind for this track — it is never an error being swallowed. Any
real failure to reach or parse the provider's response on `/upnext`,
`/lyrics` and `/related` is always 502 or 504.

The four `null` typed credit sections with `other_sections: []` on
`/credits` are the one exception to that last sentence: on a track the
ADR 003 probe confirms exists, a navigation failure while parsing the
credits dialog is reported as this same 200 empty, not a 502. See
`docs/adr/004-missing-credits-dialog-is-an-expected-empty.md`.

`data.tracks[].duration_seconds` (on `/upnext`) and
`data.songs[].duration_seconds` (on `/related`) are both nullable. On
`/upnext` the value comes from the provider's numeric field when
present, and otherwise from parsing its text duration (`"3:07"`); a
value that cannot be parsed returns `null` rather than an error.

`/lyrics` and `/related` each make **two** chained calls to the
provider by design: the browse id for the second call comes out of the
first (the watch playlist). `/credits` makes a single call, building
its own browse id instead of getting it from a chained call. None of
the four routes makes a call per item.

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
- `/credits` `data`: `performed_by`, `written_by`, `produced_by`,
  `music_metadata_provided_by` (each nullable, an object with
  `localized_title` and `names`), `other_sections` (a list of the same
  object shape, never `null`).
