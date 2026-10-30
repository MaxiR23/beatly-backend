## GET /recommendations

Returns the personalized recommendations stored for the authenticated user:
artists and albums similar to what the user saves and plays. It only reads
the database; it never calls the external provider and never generates
anything. The set is made by `POST /recommendations/generate`.

| Case | Status | Body |
|---|---|---|
| The user has a stored set | 200 | `ok: true`, `data.artists`, `data.albums`, `data.generated_at`, `data.valid_until` |
| No set yet | 200 | `ok: true`, `data.artists: []`, `data.albums: []`, `data.generated_at: null`, `data.valid_until: null` |
| Set already expired | 200 | the stored set as it is, with `valid_until` in the past |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| A stored row does not fit the model | 502 | `ok: false`, `reason: "upstream_error"` (never an empty list) |

No query params and no body; the user is always the one in the token. The
list is not paginated: each section has a fixed cap of 15 items that does
not grow with use, like the sections of `/search`.

### Shape

```json
{
  "ok": true,
  "data": {
    "artists": [
      {
        "id": "UC...",
        "name": "Artist",
        "thumbnail_url": "https://.../abc=w544-h544-l90-rj-p",
        "image": {"small": "...", "medium": "...", "large": "...", "color": null},
        "reason": {
          "type": "similar_to",
          "seed": {
            "type": "artist",
            "id": "UC...",
            "name": "Seed artist",
            "thumbnail_url": "https://.../seed=w544-h544-l90-rj-p",
            "image": {"small": "...", "medium": "...", "large": "...", "color": null}
          }
        }
      }
    ],
    "albums": [
      {
        "id": "MPREb_...",
        "title": "Album",
        "artists": [{"id": "UC...", "name": "Artist"}],
        "year": "2021",
        "audio_playlist_id": "OLAK5uy_...",
        "thumbnail_url": "https://...",
        "image": {"small": "...", "medium": "...", "large": "...", "color": null},
        "reason": {"type": "similar_to", "seed": {"type": "album", "id": "MPREb_...", "name": "Album title", "thumbnail_url": "...", "image": {"...": "..."}}}
      }
    ],
    "generated_at": "2026-10-30T12:00:00+00:00",
    "valid_until": "2026-11-29T12:00:00+00:00"
  }
}
```

- `artists` has the shape of an artist reference (`id`, `name`,
  `thumbnail_url`, `image`) plus `reason`. `albums` has the shape of an album
  reference (the one in `/album/{id}` `related_recommendations`) plus
  `reason`. Both lists are ordered by their stored rank.
- `reason` is why the item is recommended, **not** the `reason` of an error
  envelope (that one only exists when `ok` is `false`). `reason.type` is
  always `similar_to`. `reason.seed` is the artist or album it is similar to;
  `seed.type` is `artist` or `album`, and the seed carries `image` like any
  other image.
- `generated_at` and `valid_until` are ISO 8601 strings with offset, as the
  database returns them, and are `null` only when there is no set. All the
  items of a set share both.
- `valid_until` in the past does not hide the set: the client decides
  whether to call `POST /recommendations/generate`.

### Image color

Every `image` carries `color`: a lowercase `"#rrggbb"` or `null`, in the
items and in the seeds. It is `null` until the server has computed it (the
first time it sees the picture) and a later request carries it; the
computation happens after the response is sent. See
[Image tiers](conventions.md#image-tiers) and [Color](conventions.md#color).
Reading the stored colors never fails the request: on a database failure
`color` is `null` and the rest of the body is the same.

### Headers

Every response, `GET` and `POST`, carries `Cache-Control: private,
no-cache` on success and `no-store` on an error.

## POST /recommendations/generate

Generates the user's recommendations, stores them and returns them. No body,
no query: `POST` with a `user_id` in either of them is accepted and the
value is ignored; the user is the one in the token. The response has the
same `data` as `GET /recommendations`, the same shape. When the `POST` writes,
or returns the stored set that is still valid, it is that same read, with the
same timestamps. In the empty outputs below it returns `null` timestamps,
and a later `GET` can still show the previous set.

| Case | Status | Body |
|---|---|---|
| Generated | 200 | `ok: true`, `data` as in `GET`, 15 artists and 15 albums when there are enough candidates |
| The stored set is still valid (`valid_until` in the future) | 200 | the stored set, unchanged; nothing is generated, the provider is not called |
| The stored set is expired | 200 | a new set that replaces the old one |
| No seeds (no favorite artists and no plays), or every seed is gone from the provider | 200 | `data.artists: []`, `data.albums: []`, both timestamps `null`; nothing is written and an existing set is left as it is |
| Seeds found but no candidate at all (no eligible similar artist and no album) | 200 | `data.artists: []`, `data.albums: []`, both timestamps `null`; nothing is written and an existing set is left as it is |
| Fewer than 15 candidates | 200 | the artists or albums found (fewer than 15) |
| A seed, a similar artist or a history album that the provider confirms does not exist | 200 | skipped; the others fill the set |
| Provider failed (not a confirmed missing id) | 502 | `ok: false`, `reason: "upstream_error"`; nothing is written |
| Provider timed out, or the 12 s request budget ran out | 504 | `ok: false`, `reason: "upstream_timeout"`; nothing is written |
| Database failed (reading the inputs or writing the set) | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |

A failure never leaves a partial set: everything is computed first and the
old rows are replaced by the new ones in a single database transaction. A
failed `POST` leaves the previous set as it was, and `GET` keeps returning
it.

### How the set is made

1. Seeds. Up to 5 artist seeds, alternating the user's favorite artists
   (library items of kind `artist`, most recently saved first) and the
   artists they played most in the last 30 days, without repeats; if one
   list runs out the other goes on. Up to 3 album seeds, the most played
   albums of the last 30 days. An id the `/artist` or `/album` route would
   reject is not a seed.
2. Artists. Each artist seed contributes its related artists. Never
   recommended: any favorite, any of the 20 most played artists of the last
   90 days, any artist played 3 or more times in the last 90 days, or any
   seed. The rules add up: an artist with 2 plays that is in the top 20 is
   still excluded. Candidates are taken in rounds, one per seed in
   turn, until there are 15, so with 5 seeds each gets 3 and with 1 seed that
   seed gets all 15. A seed with fewer candidates simply gives what it has.
3. Albums. The albums of an artist seed come from the albums of its chosen
   similar artists, one from each in turn. An album seed contributes the
   albums its page recommends. The album seeds take the closing slots first
   (one album each), the artist seeds fill the rest by rounds, and if some are
   still missing the album seeds add more. The seed albums themselves are not
   recommended.
4. Missing ids. When the provider answers with a page that confirms the
   artist or album does not exist, that id is skipped (one `INFO` line in the
   log), and the rounds spread over the rest: with 4 valid artist seeds the
   artists come 4, 4, 4 and 3. Only that confirmation skips an id; any other
   failure of the provider is a 502 or 504 and nothing is written.

### How long a set is valid

`valid_until` is `generated_at` plus a number of days that depends on how
many plays the user made since the previous generation (since 30 days ago
the first time):

| Plays since the previous generation | Days valid |
|---|---|
| less than 5 | 30 |
| 5 to 19 | 14 |
| 20 or more | 7 |

All the numbers come from the environment, validated at startup (documented
in `.env.example`; a wrong value fails `Settings()` without echoing it):

| Variable | Default | Meaning |
|---|---|---|
| `RECOMMENDATIONS_TIER_MID_PLAYS` | 5 | plays for the middle tier (below `HIGH_PLAYS`) |
| `RECOMMENDATIONS_TIER_HIGH_PLAYS` | 20 | plays for the shortest validity |
| `RECOMMENDATIONS_TIER_HIGH_DAYS` | 7 | days valid at `HIGH_PLAYS` or more |
| `RECOMMENDATIONS_TIER_MID_DAYS` | 14 | days valid at `MID_PLAYS` or more |
| `RECOMMENDATIONS_TIER_LOW_DAYS` | 30 | days valid below `MID_PLAYS` |
| `RECOMMENDATIONS_SEED_WINDOW_DAYS` | 30 | window of the listened seeds, the history albums and the first count of plays |
| `RECOMMENDATIONS_EXCLUDE_WINDOW_DAYS` | 90 | window of the top artists and of the play threshold that are never recommended |
| `RECOMMENDATIONS_EXCLUDE_TOP_ARTISTS` | 20 | how many of those top artists (0 turns it off) |
| `RECOMMENDATIONS_EXCLUDE_MIN_PLAYS` | 3 | an artist played this many times or more in that window is never recommended (1 or more) |
| `RECOMMENDATIONS_HISTORY_ALBUMS` | 3 | most played albums used as album seeds (0 to 15) |
| `RECOMMENDATIONS_PARALLEL_CALLS` | 4 | simultaneous provider calls in one generation (1 to 8) |

The server does not start if `MID_PLAYS` is not below `HIGH_PLAYS`, or if
`HIGH_DAYS < MID_DAYS < LOW_DAYS` does not hold.

### Time

The generation happens inside the request: about 23 provider calls in three
stages (the seeds' pages, the similar artists' pages, the history albums),
made 4 at a time and sharing the 12 s request budget of
[Time limits](conventions.md#time-limits). Measured: 5.4 s in series, 2.1 s
with 4 at a time, against the 12 s budget; the worst case with every stage
paying the slowest call measured is about 5.4 s. That assumes no id is missing: each missing id adds a probe call and each missing similar artist another round, within the same budget (a 504 if it runs out). The pages use the same
server-side cache as `/artist/{id}`. The numbers and why it is not a
background job: `docs/adr/025-recommendations-are-generated-inside-the-request-in-parallel.md`.
Two `POST` at the same time for the same user do the work twice; the writes
are serialized and the last one wins, without an error.

### Storage

The set is stored in `user_recommendations`, bucket `for_you`, source
`similar_v1`. The table is only read and written by the backend (migration
047). A `POST` also deletes any older row of the user, whatever its bucket.
The stored payload keeps the names, titles and thumbnail URLs, never the
image tiers, which are derived on every response.
