## GET /album/{album_id}

Looks up an album on the external provider by id and returns it along
with its tracks. Authenticated: requires a Supabase JWT like the rest
of the API.

| Case | Status | Body |
|---|---|---|
| Album found | 200 | `ok: true`, `data` with the album and its tracks |
| `album_id` does not start with the required prefix | 422 | `ok: false`, `reason: "invalid_request"` |
| Missing or invalid token | 401 | `ok: false`, `reason: "unauthorized"` |
| `album_id` is well formed but no album matches it | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider failed, including a response whose layout could not be parsed | 502 | `ok: false`, `reason: "upstream_error"` |
| The external provider timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
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

`data.tracks`, `data.other_versions` and `data.related_recommendations`
are returned whole, unpaginated, exactly as the external provider's
single response for this album carries them, with the same reasoning
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
  `track_number`. `track_id` and `duration_seconds` are `null` for a
  track the provider marks unavailable (region-locked or taken down):
  it still appears in the list rather than being dropped, with
  `is_available: false`, so the numbering has no gaps and the caller
  can gray it out instead of hiding it. `track_number` is never `null`:
  it comes from the provider when present and falls back to the
  track's position in the list otherwise.
- Each element of `data.other_versions` and `data.related_recommendations`:
  `id` (the provider's `browseId`), `title`, `artists`, `year`
  (nullable), `audio_playlist_id` (nullable), `thumbnail_url`
  (nullable).
- `artists`, on the album, on each track and on each referenced album,
  is a list of `{id, name}` and is `[]`, never `null`, when the
  external provider has no artist data for it (an album with no
  strapline, and the tracks that inherit that absence).
