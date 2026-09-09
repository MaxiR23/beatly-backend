## GET /album/{album_id}

Looks up an album on the external provider by id and returns it along
with its tracks. Authenticated: requires a Supabase JWT like the rest
of the API.

| Case | Status | Body |
|---|---|---|
| Album found | 200 | `ok: true`, `data` with the album and its tracks |
| Album found, `audio_playlist_id` is `null` | 200 | `ok: true`, `data.tracks: []`, the rest of the album populated as usual |
| `album_id` does not start with the required prefix | 422 | `ok: false`, `reason: "invalid_request"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| `album_id` is well formed but no album matches it | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider failed, including a response whose layout could not be parsed, on either the album call or the audio playlist call | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out, on either call | 504 | `ok: false`, `reason: "upstream_timeout"` |
| `GET /album/` with no id | 404 | `ok: false`, `reason: "not_found"` — no route matches |

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

`data.tracks` comes from the album's **audio playlist**
(`audio_playlist_id`/`audioPlaylistId`), a second call to the external
provider, not from the album's own payload. This endpoint makes **two**
calls to the provider on the happy path — the album lookup and the
audio playlist lookup — and never a call per track. The reason is the
provider's own inconsistency: the album payload's track list mixes
music-video ids with audio-track ids for the same songs (measured live:
176 of 389 sampled items were music-video ids), while every item of the
audio playlist is an audio-track id (measured: 389 of 389). Reading from
the audio playlist instead is what makes every `track_id` this endpoint
returns usable as-is with `GET /tracks/{track_id}/credits` — see
`docs/api/tracks.md`.

With `audio_playlist_id: null`, the response is still **200** with
`data.tracks: []` and the rest of the album populated normally — an
expected empty, never a 404 and never a 502. This was not observed live
(0 of 26 albums measured) and is logged as a warning on the server,
carrying the album_id and nothing from the provider, when it happens.
There is deliberately **no
fallback** to the album payload's own track list for this case: falling
back would reintroduce the music-video ids this endpoint exists to stop
returning, and `data.tracks: []` would no longer mean one thing. An
audio playlist the provider cannot deliver at all (a network failure, a
timeout, or a layout it cannot parse) is **502**/**504** instead, never
a silent `tracks: []`, so an empty list always means "no audio playlist
id", nothing else.

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
  `thumbnail_url` (nullable), `tracks`, `other_versions`,
  `related_recommendations`.
- Each element of `data.tracks`: `track_id` (nullable), `title`,
  `artists`, `duration_seconds` (nullable), `is_available`,
  `track_number`. `track_id` is the id of the **audio track**, taken
  from the audio playlist item, and works as-is with
  `GET /tracks/{track_id}/credits`. `track_id` and `duration_seconds`
  are `null` for a track the provider marks unavailable (region-locked
  or taken down): it still appears in the list rather than being
  dropped, with `is_available: false`, so the numbering has no gaps and
  the caller can gray it out instead of hiding it. `track_number` is
  never `null`: it is always the track's 1-based position in the list,
  because the audio playlist this list comes from never carries the
  provider's own track number.
- Each element of `data.other_versions` and `data.related_recommendations`:
  `id` (the provider's `browseId`), `title`, `artists`, `year`
  (nullable), `audio_playlist_id` (nullable), `thumbnail_url`
  (nullable).
- `artists`, on the album and on each referenced album, is a list of
  `{id, name}` and is `[]`, never `null`, when the external provider
  has no artist data for it (an album with no strapline). On each
  track, `artists` is whatever its audio playlist item carries, `[]`
  when the item has none — it does **not** fall back to the album's
  own artists (measured live: 0 of 485 tracks sampled across 18
  compilation/soundtrack albums needed to).
