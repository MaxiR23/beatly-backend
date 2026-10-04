## GET /album/{album_id}

Looks up an album on the external provider by id and returns it along
with its tracks. Authenticated: requires a Supabase JWT like the rest
of the API.

| Case | Status | Body |
|---|---|---|
| Album found | 200 | `ok: true`, `data` with the album and its tracks |
| Album found, at least one of its tracks unavailable | 200 | `ok: true`, `data.tracks` from the album's own track list, unavailable tracks kept; the audio playlist is not requested |
| Album found, `audio_playlist_id` is `null` | 200 | `ok: true`, `data.tracks` built from the album's own track list (`[]` if that has none), the rest of the album populated as usual |
| `album_id` does not start with the required prefix | 422 | `ok: false`, `reason: "invalid_request"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| `album_id` is well formed but no album matches it | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider failed, including a response whose layout could not be parsed, on either the album call or the audio playlist call | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out, on either call | 504 | `ok: false`, `reason: "upstream_timeout"` |
| `GET /album/` with no id | 404 | `ok: false`, `reason: "not_found"` — no route matches |

A successful response may be served from a server-side Redis cache and be
up to **24 hours** stale relative to the external provider. The cache
never changes the response's shape, status or reason, and a Redis
failure is invisible to the client: the endpoint responds with the same
body, status and reason as it would with no cache at all. See
`docs/adr/005-provider-cache-lives-in-the-services.md` for why.

The successful response carries `Cache-Control: max-age=<seconds left>`,
at most the operation's TTL (24 hours); see [Caching headers](conventions.md#caching-headers).

`album_id` is the external provider's id for the album and is required
to start with the prefix the provider itself expects (`MPRE`); an id
that does not match it is rejected with 422 before any outgoing call is
made.

A well-formed but nonexistent `album_id` responds **502, never 404**.
See `docs/adr/002-nonexistent-album-id-maps-to-upstream-error.md` for
why: the external provider's response for an unknown id and for a
layout it can no longer parse both surface as the same underlying
failure, with nothing in the payload to tell them apart, so this
endpoint has no domain 404.

`data.id` is always the id that was requested, not one read back from
the external provider's response, which never includes it.

When the album has an `audio_playlist_id` and every track in its own
payload is available, `data.tracks` comes from the album's **audio playlist**
(`audio_playlist_id`/`audioPlaylistId`), a second call to the external
provider, not from the album's own payload. This endpoint makes **two**
calls to the provider when it has an audio playlist id and every track
is available (one otherwise, see below) — the album lookup and the audio playlist lookup — and
never a call per track. The reason is the
provider's own inconsistency: the album payload's track list mixes
music-video ids with audio-track ids for the same songs (measured live:
176 of 389 sampled items were music-video ids), while every item of the
audio playlist is an audio-track id (measured: 389 of 389). Reading from
the audio playlist instead is what makes every `track_id` this endpoint
returns usable as-is with `GET /tracks/{track_id}/credits` — see
`docs/api/tracks.md`.

### When some tracks are unavailable

If any track in the album's own payload has `isAvailable: false`,
`data.tracks` is built from that same payload and the audio playlist is
**not requested**: the choice comes from the album response, with no
second request. The reason is measured: with DAMN. (14 tracks, 9
unavailable) the provider's audio playlist arrives without content and
cannot be read. The rule is by availability, not by whether the audio
playlist would have worked: an album with at least one unavailable track
in its payload is served from its own track list even if its audio
playlist could have served it whole. That has consequences for
`track_id` (it can be a music-video id), `artists` and
`duration_seconds`, as follows. In this list:

- `track_id` of an available track can be a **music-video id** (measured:
  the 5 available tracks of DAMN. are all video ids with no audio
  counterpart), so with `GET /tracks/{track_id}/credits` it answers 200
  with null sections, see `docs/api/tracks.md`.
- `track_id` is `null` for an unavailable track, and `duration_seconds`
  is the album's own duration, also for unavailable tracks.
- `track_number` is the 1-based position in the list.
- A track with no artists of its own carries the album's artists (the
  provider copies them into that list).
- A warning with the album_id and nothing from the provider is logged.
- The response is cached the same 24 hours.

An album with no tracks in its payload does not enter this rule: it
follows the audio playlist, and if that fails the response is 502,
never a silent `tracks: []`.

With `audio_playlist_id: null` (or an empty string), the response is
still **200** and `data.tracks` is built from the album's own track list,
with the same rule as the unavailable case above (available tracks carry
the payload's `track_id`, unavailable ones `null`, the real
`duration_seconds`, the 1-based position as `track_number`), whether or
not some tracks are unavailable. The audio playlist is not requested, and
`track_id` can be a music-video id, as above. If the album's payload has
no tracks either, `data.tracks` is `[]`: an expected empty, never a 404
and never a 502. This was not observed live (0 of 26 albums measured) and
is logged as a warning on the server, carrying the album_id and nothing
from the provider, when it happens. An audio playlist the provider cannot
deliver at all (a network failure, a timeout, or a layout it cannot
parse) when there is an `audio_playlist_id` is **502**/**504** instead,
never a silent `tracks: []`.

`data.tracks`, `data.other_versions` and `data.related_recommendations`
are returned whole, unpaginated, exactly as the external provider's
responses for this album carry them, with the same reasoning
`docs/api/search.md` and `docs/api/playlists.md` document: the album is
the payload of this endpoint, not a growable collection in its own
right, so none of the three is wrapped in the `Paginated[T]` envelope.

`description` (and `descriptionRuns`) is deliberately not exposed: it
is third-party editorial content with its own attribution, and this API
does not carry it.

Fields:

- `data`: `id`, `title`, `year` (nullable), `artists`, `track_count`
  (nullable), `duration_seconds`, `audio_playlist_id` (nullable),
  `thumbnail_url` (nullable; 544 x 544, smart crop; see [Image size](conventions.md#image-size)), `tracks`,
  `other_versions`, `related_recommendations`.
- Each element of `data.tracks`: `track_id` (nullable), `title`,
  `artists`, `duration_seconds` (nullable), `is_available`,
  `track_number`. `track_id` is the id of the **audio track**, taken
  from the audio playlist item, and works as-is with
  `GET /tracks/{track_id}/credits`, except when the list comes from the
  album's own track list (above). `track_id` is `null` for a track the
  provider marks unavailable (region-locked or taken down), and so is
  `duration_seconds` when the list comes from the audio playlist; from
  the album's own list it keeps the album's duration. The track still appears in the list rather than being
  dropped, with `is_available: false`, so the numbering has no gaps and
  the caller can gray it out instead of hiding it. `track_number` is
  never `null`: it is always the track's 1-based position in the list,
  because the audio playlist never carries the provider's own track
  number and the album's own list has it empty for unavailable tracks.
- Each element of `data.other_versions` and `data.related_recommendations`:
  `id` (the provider's `browseId`), `title`, `artists`, `year`
  (nullable), `audio_playlist_id` (nullable), `thumbnail_url`
  (nullable; 544 x 544, smart crop; see [Image size](conventions.md#image-size)).
- `artists`, on the album and on each referenced album, is a list of
  `{id, name}` and is `[]`, never `null`, when the external provider
  has no artist data for it (an album with no strapline). On each
  track, `artists` is whatever its audio playlist item carries, `[]`
  when the item has none — it does **not** fall back to the album's
  own artists (measured live: 0 of 485 tracks sampled across 18
  compilation/soundtrack albums needed to), except in the album's own
  track list (above), where the provider already copies them.
