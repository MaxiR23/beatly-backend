## POST /ingest/genres

Internal: not used by the app clients. Only the collector calls it, with
`INGEST_TOKEN` (see [the ingest guide](../ingest.md)). It replaces the
playlists of the genres in the body, all or nothing, with the result the
collector built on the owner's phone.

Authentication: `Authorization: Bearer <INGEST_TOKEN>`. It is a single static
token, compared in constant time, and it is not a user JWT: a user token is
401. When `INGEST_TOKEN` is not configured in the back, the endpoint answers
401 to every request. The token is checked before the body is read: without a
valid token the answer is 401 whatever the body is, even a malformed JSON.

| Case | Status | Body |
|---|---|---|
| Valid submission, written | 200 | `ok: true`, `data: {"genres": n, "playlists": n, "tracks": n}` |
| No `Authorization`, another scheme, a wrong token, or `INGEST_TOKEN` not configured | 401 | `ok: false`, `reason: "unauthorized"` |
| Body that is not valid JSON, invalid, incomplete, incoherent or over a limit | 422 | `ok: false`, `reason: "invalid_request"` |
| A slug of the body matches no genre | 404 | `ok: false`, `reason: "genre_not_found"` |
| The database failed, or the function returned another count than the one sent | 502 | `ok: false`, `reason: "upstream_error"` |
| The database did not answer within `INGEST_RPC_TIMEOUT` | 504 | `ok: false`, `reason: "upstream_timeout"` |

There is no valid empty submission: an ingest with no genres, or a playlist
with no tracks, is a 422 and nothing is written. The 422 carries no detail of
what was wrong; the reason is in the server log (the count of errors only).

### Body

```json
{
  "genres": [
    {
      "slug": "rock",
      "playlists": [
        {
          "source_playlist_id": "PL...",
          "title": "Rock classics",
          "description": null,
          "category": null,
          "tracks": ["<track_id>", "<track_id>"]
        }
      ]
    }
  ],
  "tracks": [
    {
      "track_id": "<track_id>",
      "title": "Song",
      "artists": [{"id": "UC...", "name": "Artist"}],
      "album": "Album",
      "album_id": "MPREb_...",
      "duration_seconds": 215,
      "thumbnail_url": "https://..."
    }
  ]
}
```

- A playlist lists its tracks by id, in order; the rows of `tracks` go once
  and hold the metadata. `sort_order` of a playlist (inside its genre,
  from 1) and `position` of a track (inside its playlist, from 1) are not in
  the body: they come from the order of the lists.
- Every model rejects unknown keys.
- `description` and `category` are optional (`null` or absent).
- Limits: up to 50 genres, 50 playlists per genre, 500 tracks per playlist and
  20 000 distinct tracks in `tracks`; ids (`track_id`, `source_playlist_id`,
  `album_id`) 1 to 128 characters of letters, digits, `_` and `-`; titles and
  `album` 1 to 300 characters; a playlist `title` up to 200, `description` up
  to 2 000 and `category` 1 to 100; `thumbnail_url` an `https://` URL up to
  2 048 characters; `duration_seconds` 1 to 86 400; up to 20 artists per track;
  a slug without whitespace, commas, parentheses or quotes, up to 100.
- The first artist of every track has an id (the rule of every song of the
  app, see `conventions.md`).
- Coherence: no repeated slug; no repeated `source_playlist_id` inside a
  genre; no repeated `track_id` inside a playlist or in `tracks`; every id a
  playlist references has a row in `tracks`, and every row of `tracks` is
  referenced by some playlist (an orphan row is an incoherent submission).
- There is no limit on the size of the body in bytes. The largest submission
  the limits allow is about 4.5 MB.

### What it writes

Scope and identity:

- Only the genres in the body are touched. Each playlist is identified by its
  `source_playlist_id` inside its genre (migration 052): a playlist that
  already exists keeps its `id`, and only its tracks are replaced.
- The playlists of those genres that are not in the body are deleted, and so
  are the playlists the genre already had before the column existed (the
  historical seed): they are playlists that are not in the body. Their `id`s
  answer 404 `playlist_not_found` afterwards.
- A genre that is not in the body is not touched. If the collector does not
  run, the app keeps serving what was saved.
- Every `tracks` row is written with `ON CONFLICT (track_id) DO UPDATE` of
  `title`, `artists`, `album`, `album_id`, `duration_seconds` and
  `thumbnail_url`, as `POST /likes` does for a liked track.
- `track_count` of a playlist is the number of tracks saved, and its
  `thumbnail_url` the cover of the first saved track that has one.

All or nothing: the whole submission is one transaction in the database
(`replace_genre_playlists`). Any failure leaves the previous playlists and
tracks exactly as they were.

The same body leaves the same result: the same playlist ids, the same tracks
in the same positions and the same counts. It is safe to send it again, which
the collector does when it does not get an answer. Two submissions at the same
time are serialized by a lock in the function instead of failing.

### Time

The write is one long call to the database with its own read timeout,
`INGEST_RPC_TIMEOUT` (60 seconds by default, at most 300), longer than the
app's. A 504 can mean the write did happen and the answer was late: send the
same body again. The platform's request timeout has to be above
`INGEST_RPC_TIMEOUT` (see `docs/deploy.md`).

### Headers

Every response carries `Cache-Control: no-store`.

## POST /ingest/releases

Internal: not used by the app clients. Only the collector calls it, with
`INGEST_TOKEN` (see [the ingest guide](../ingest.md)). It replaces the whole
list of new releases of its source, all or nothing, with the cards the
collector read on the owner's phone and kept.

Authentication: the same as `POST /ingest/genres`. `Authorization: Bearer
<INGEST_TOKEN>`; a user JWT is 401; with `INGEST_TOKEN` not configured every
request is 401; the token is checked before the body is read.

| Case | Status | Body |
|---|---|---|
| Valid submission, written | 200 | `ok: true`, `data: {"releases": n}` |
| No `Authorization`, another scheme, a wrong token, or `INGEST_TOKEN` not configured | 401 | `ok: false`, `reason: "unauthorized"` |
| Body that is not valid JSON, invalid or over a limit | 422 | `ok: false`, `reason: "invalid_request"` |
| The database failed, or the function returned another count than the one sent | 502 | `ok: false`, `reason: "upstream_error"` |
| The database did not answer within `INGEST_RPC_TIMEOUT` | 504 | `ok: false`, `reason: "upstream_timeout"` |

There is no 404: the body references no existing row. There is no valid empty
submission: an empty list is a 422 and nothing is written (an empty list would
delete what the app shows). The 422 carries no detail; the server log has the
count of errors only.

### Body

```json
{
  "releases": [
    {
      "album_id": "MPREb_...",
      "audio_playlist_id": "OLAK5uy_...",
      "type": "album",
      "title": "Album",
      "artists": [{"id": "UC...", "name": "Artist"}],
      "thumbnail_url": "https://..."
    }
  ]
}
```

- `position` and `source` are not in the body: the position comes from the
  order of the list (from 1) and the source is fixed by the function.
- Every model rejects unknown keys.
- `album_id` matches `MPREb_` followed by letters, digits, `_` and `-`;
  `audio_playlist_id` matches `OLAK5uy_` and the same characters; both up to
  128 characters. `artists[].id` is `UC` followed by 22 of those characters.
- `type` is `album`, `single` or `ep`, already in lowercase.
- `title` and `artists[].name` are 1 to 300 characters; a name that is blank or
  is `null`, `none` or `undefined` (any case, spaces around ignored) is
  rejected.
- 1 to 20 artists per release; `thumbnail_url` an `https://` URL up to 2 048
  characters; 1 to 500 releases; no repeated `album_id`.

### What it writes

- It replaces the list of its source (`provider_new_releases`) in one
  transaction (`replace_new_releases`, migration 053): the previous rows of the
  source are deleted and the new ones inserted with `position` 1..n. Any
  failure leaves the previous list as it was.
- The same body twice leaves the same rows. Two submissions at the same time
  are serialized by a lock in the function.
- If the collector does not run, or aborts, the previous list stays and is
  what `GET /releases` serves.

### Time

The same long write and the same `INGEST_RPC_TIMEOUT` as `POST /ingest/genres`:
a 504 can mean the write did happen; send the same body again.

### Headers

Every response carries `Cache-Control: no-store`.
