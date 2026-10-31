## GET /recommendations

Returns the personalized recommendations stored for the authenticated user:
artists and albums similar to what the user saves and plays, split into the
sections of the home screen (#227). It only reads the database; it never
calls the external provider and never generates anything. The set is made by
`POST /recommendations/generate`.

| Case | Status | Body |
|---|---|---|
| The user has a stored set | 200 | `ok: true`, `data.albums_for_you`, `data.seed_sections`, `data.recommended_albums`, `data.recommended_artists`, the deprecated `data.artists` and `data.albums`, `data.generated_at`, `data.valid_until` |
| No set yet | 200 | `ok: true`, every list `[]`, `data.generated_at: null`, `data.valid_until: null` |
| Set already expired | 200 | the stored set as it is, with `valid_until` in the past |
| A set stored before the sections (#227) | 200 | the four new lists `[]`; `data.artists`, `data.albums` and both timestamps as stored. It has no sections until a `POST` regenerates it |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |
| A stored row does not fit the model, or its section does not fit its item (an album in `recommended_artists`, a `seed_sections` row without `section_index` from 0 to 2, an unknown section) | 502 | `ok: false`, `reason: "upstream_error"` (never an empty list) |

No query params and no body; the user is always the one in the token. The
lists are not paginated: each one has a fixed cap that does not grow with
use (10 albums for you, up to 3 sections of 6 items, 10 recommended albums,
10 recommended artists), like the sections of `/search`.

### Shape

```json
{
  "ok": true,
  "data": {
    "albums_for_you": [
      {
        "id": "MPREb_...",
        "title": "Album",
        "artists": [{"id": "UC...", "name": "Artist"}],
        "year": "2021",
        "audio_playlist_id": "OLAK5uy_...",
        "thumbnail_url": "https://...",
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
    "seed_sections": [
      {
        "seed": {
          "type": "artist",
          "id": "UC...",
          "name": "Seed artist",
          "thumbnail_url": "https://.../seed=w544-h544-l90-rj-p",
          "image": {"small": "...", "medium": "...", "large": "...", "color": null}
        },
        "items": [
          {
            "type": "artist",
            "id": "UC...",
            "name": "Similar artist",
            "thumbnail_url": "https://.../abc=w544-h544-l90-rj-p",
            "image": {"small": "...", "medium": "...", "large": "...", "color": null},
            "reason": {"type": "similar_to", "seed": {"type": "artist", "id": "UC...", "name": "Seed artist", "thumbnail_url": "...", "image": {"...": "..."}}}
          },
          {
            "type": "album",
            "id": "MPREb_...",
            "title": "Album",
            "artists": [{"id": "UC...", "name": "Artist"}],
            "year": "2019",
            "audio_playlist_id": "OLAK5uy_...",
            "thumbnail_url": "https://...",
            "image": {"small": "...", "medium": "...", "large": "...", "color": null},
            "reason": {"type": "similar_to", "seed": {"type": "artist", "id": "UC...", "name": "Seed artist", "thumbnail_url": "...", "image": {"...": "..."}}}
          }
        ]
      }
    ],
    "recommended_albums": [ { "...": "same item shape as albums_for_you" } ],
    "recommended_artists": [
      {
        "id": "UC...",
        "name": "Artist",
        "thumbnail_url": "https://.../abc=w544-h544-l90-rj-p",
        "image": {"small": "...", "medium": "...", "large": "...", "color": null},
        "reason": {"type": "similar_to", "seed": {"type": "artist", "id": "UC...", "name": "Seed artist", "thumbnail_url": "...", "image": {"...": "..."}}}
      }
    ],
    "artists": [ { "...": "deprecated: every artist of the set, by rank, same shape as recommended_artists" } ],
    "albums": [ { "...": "deprecated: every album of the set, by rank, same shape as albums_for_you" } ],
    "generated_at": "2026-10-30T12:00:00+00:00",
    "valid_until": "2026-11-29T12:00:00+00:00"
  }
}
```

- `albums_for_you`, `recommended_albums` and `recommended_artists` hold
  items of the shape of an album reference (the one in `/album/{id}`
  `related_recommendations`) or of an artist reference (`id`, `name`,
  `thumbnail_url`, `image`), plus `reason`. They carry no `type`.
- `seed_sections` has up to 3 sections, one per each of the first three artist
  seeds, in the order of the seeds. `seed` is the seed (always `type:
  "artist"`) and is the same as the `reason.seed` of all its items. `items`
  alternates artist, album, artist, ... and is the only mixed list, so only
  its items carry `type`, `artist` or `album` (the names and values of
  `reason.seed.type`). A section has up to 3 artists and 3 albums.
- No artist id and no album id appears twice among `albums_for_you`,
  `seed_sections`, `recommended_albums` and `recommended_artists`. Sections
  that cannot be filled are shorter: a section with only artists or only
  albums is returned with what it has, and a section with nothing is left
  out of `seed_sections`.
- No two editions of the same album appear either: same artist and same base
  title, which is the title without its trailing groups in parentheses or
  square brackets ("Album (Deluxe)" and "Album (Expanded)" are "Album"). And
  no soundtrack is recommended. The limits of both rules are in "How the set
  is made", step 3.
- `artists` and `albums` are **deprecated**. They keep the content they had
  before (#227): every artist and every album of the set, flattened, with
  the same item shape and ordered by their stored rank, now up to 19 and 29
  items. The app's onboarding validates them today, so they stay until the
  new home screen uses the other four lists; a later issue removes them.
  Every item travels twice, in its section and in these lists.
- `reason` is why the item is recommended, **not** the `reason` of an error
  envelope (that one only exists when `ok` is `false`). `reason.type` is
  always `similar_to`. `reason.seed` is the artist or album it is similar to
  (for the items of a section, the seed of the section; for the lists, the
  seed it really came from, any of the 5, or an album seed of the history);
  `seed.type` is `artist` or `album`, and the seed carries `image` like any
  other image.
- `generated_at` and `valid_until` are ISO 8601 strings with offset, as the
  database returns them, and are `null` only when there is no set. All the
  items of a set share both.
- `valid_until` in the past does not hide the set: the client decides
  whether to call `POST /recommendations/generate`.
- A set stored before the sections has no section data: until a `POST`
  regenerates it, the four new lists come back empty even though
  `valid_until` is in the future. A client that shows the sections can call
  `POST /recommendations/generate` when the home screen opens: with a valid
  set that has sections it does not call the provider and returns the stored
  set, and with an old one it regenerates it.

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
same `data` as `GET /recommendations`, the same shape (the four lists, the
two deprecated ones and the timestamps). When the `POST` writes,
or returns the stored set that is still valid, it is that same read, with the
same timestamps. In the empty outputs below it returns `null` timestamps,
and a later `GET` can still show the previous set.

| Case | Status | Body |
|---|---|---|
| Generated | 200 | `ok: true`, `data` as in `GET`: 10 `albums_for_you`, up to 3 `seed_sections` of 6 items (3 artists and 3 albums), 10 `recommended_albums` and 10 `recommended_artists` when there are enough candidates; the deprecated `artists` (19) and `albums` (29) |
| The stored set is still valid (`valid_until` in the future) and has sections | 200 | the stored set, unchanged; nothing is generated, the provider is not called |
| The stored set is still valid but was stored before the sections (#227) | 200 | it counts as expired: a new set with sections replaces it |
| The stored set is expired | 200 | a new set that replaces the old one |
| No seeds (no favorite artists and no plays), or every seed is gone from the provider | 200 | every list `[]`, both timestamps `null`; nothing is written and an existing set is left as it is |
| Seeds found but no candidate at all (no eligible similar artist and no album) | 200 | every list `[]`, both timestamps `null`; nothing is written and an existing set is left as it is |
| Fewer seeds or fewer candidates | 200 | shorter lists and fewer sections (also because editions and soundtracks are skipped): with 1 seed there is 1 section, with 2 there are 2; a section with only part of its content keeps what it has and is not filled with the other type; a section with nothing is left out |
| A seed, a similar artist or a history album that the provider confirms does not exist | 200 | skipped; the others fill the set |
| Provider failed (not a confirmed missing id) | 502 | `ok: false`, `reason: "upstream_error"`; nothing is written |
| Provider timed out, or the 12 s request budget ran out | 504 | `ok: false`, `reason: "upstream_timeout"`; nothing is written |
| Database failed (reading the inputs or writing the set), or the stored set read back has a section that does not fit | 502 | `ok: false`, `reason: "upstream_error"` |
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
   turn, until there are 19 (3 for each of the first three seeds, plus 10
   recommended artists; with fewer than 3 seeds, 3 per seed plus 10). With 5
   seeds they come 4, 4, 4, 4 and 3; with 1 seed that seed gets 13 and with
   2 seeds, 16. A seed with fewer candidates simply gives what it has, and
   then only the artists that fit a list are fetched: the 19 are a ceiling,
   not a quota.
3. Albums. The albums of an artist seed come from the albums of its chosen
   similar artists, one from each in turn. An album seed contributes the
   albums its page recommends. The album seeds take the closing slots first
   (one album each, never more than the list holds), the artist seeds fill
   the rest by rounds, and if some are still missing the album seeds add
   more. The seed albums themselves are not recommended. Soundtracks are not
   candidates, from any source: a title with "Soundtrack" or "OST" as a whole
   word, in any case ("Original Soundtrack" is covered), is dropped before the
   albums are given out and the next candidate takes its place. The limits of
   the rule are literal: an album that is not a soundtrack but has the word
   ("Soundtrack to My Life") is excluded too; "Original Score", "Music from
   the Motion Picture", "O.S.T." and "B.S.O." are not excluded; "Soundtracks"
   (plural) is not, because the match is by whole word. Artists known for
   soundtracks can still be recommended as artists.
4. Sections and no repeats. The items are given out in this order, and each
   item goes to the first list that asks for it: `albums_for_you` (the 10
   best ranked albums; the album seeds of the play history hold the closing
   slots, as set in step 3), then the 3
   sections (each from one of the first three artist seeds, in order: its 3
   first chosen similar artists and 3 of the albums of its similars), then
   `recommended_albums` (the next 10 of the same ranking) and finally
   `recommended_artists` (the chosen artists that did not enter a section,
   up to 10). That is what keeps any artist or album from appearing twice.
   The sections use only artist seeds. "The first three" are the first three
   seeds whose page exists; if one of them ends up with nothing, its section
   is left out and the fourth seed does not take its place.

   Editions count as the same album. Each album carries the keys (artist,
   base title); the base title drops, repeatedly, the groups in parentheses
   or square brackets at the end, lowercases and collapses spaces ("(Untitled)"
   stays whole; a group at the start or in the middle is kept). The artist is
   each linked artist of the album (without the `MPLA` prefix) plus, for an
   album taken from an artist page, the owner of that page, because the
   linked artists may be missing there. The first edition that a list asks
   for, in the order above, keeps the album; the others enter no list and the
   next candidate takes their place. An edition of an album seed is not
   recommended either, but only if the seed's album page was found and has
   an artist with an id (the edition key needs an artist). Limits: two albums of the same artist
   that differ only in the final suffix ("Greatest Hits (1970-1980)" and
   "(1980-1990)") are treated as editions and only one is recommended;
   suffixes with a dash ("Album - Deluxe Edition"), nested parentheses and
   different apostrophes are not detected; an album with no linked artist
   that does not come from an artist page is only deduplicated by id; albums
   of different artists are never merged, even with the same title. Sets
   already stored keep their content until they expire or are regenerated.
5. Missing ids. When the provider answers with a page that confirms the
   artist or album does not exist, that id is skipped (one `INFO` line in the
   log), and the rounds spread over the rest: with 4 valid artist seeds the
   artists come 5, 5, 5 and 4. Only that confirmation skips an id; any other
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

The generation happens inside the request: 27 provider calls with 5 artist
seeds and 3 album seeds (5 seed pages, 19 similar artists' pages, 3 history
albums), in three stages made 4 at a time (8 batches: 2 + 5 + 1) and sharing
the 12 s request budget of [Time limits](conventions.md#time-limits).
Measured (12 runs, 3 sets of seeds, a new client per run): 7.2 s in series,
2.5 s with 4 at a time (2.43 to 2.66 s) and 1.9 s with 6, against the 12 s
budget; the worst case with every batch paying the slowest call measured is
6.5 s with 4 at a time. That assumes no id is missing: each missing id adds a
probe call and each missing similar artist another round, within the same
budget (a 504 if it runs out). The pages use the same server-side cache as
`/artist/{id}`. The numbers and why it is not a background job:
`docs/adr/025-recommendations-are-generated-inside-the-request-in-parallel.md`
and, for the 27 calls,
`docs/adr/026-recommendations-are-split-into-sections-without-repeats.md`.
Two `POST` at the same time for the same user do the work twice; the writes
are serialized and the last one wins, without an error.

### Storage

The set is stored in `user_recommendations`, bucket `for_you`, source
`similar_v1`. The table is only read and written by the backend (migration
047). A `POST` also deletes any older row of the user, whatever its bucket.
The stored payload keeps the names, titles and thumbnail URLs, never the
image tiers, which are derived on every response. It also keeps where the
row goes in the response: `section` (`albums_for_you`, `seed_sections`,
`recommended_albums` or `recommended_artists`) and, for `seed_sections`,
`section_index` from 0 to 2. There is no migration: the unique index
`(user_id, bucket, item_type, item_id)` is enough because no id repeats. A
set has up to 48 rows (19 artists and 29 albums); the `rank` is global per
type in the order of the screen, so the deprecated `artists` and `albums`
come out of the same ordered read. A set whose rows have no `section` was
stored before #227: `GET` returns it without sections and `POST` treats it as
expired.
