## GET /onboarding/artists

Lists the curated artists the onboarding shows as a grid to pick from. A
cursor-paginated list ordered by position, the order of the grid. See the
Pagination section of `conventions.md` for the shared `limit`/`cursor` query
params and the `data.items`/`data.page` shape. It only reads the database;
it never calls the external provider.

| Case | Status | Body |
|---|---|---|
| The table has artists | 200 | `ok: true`, `data.items`, `data.page` |
| The table is empty | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

### Shape

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "id": "UC...",
        "name": "Artist",
        "thumbnail_url": "https://.../abc=w544-h544-p-l90-rj",
        "image": {"small": "...", "medium": "...", "large": "...", "color": null}
      }
    ],
    "page": {"limit": 50, "next_cursor": null, "has_more": false, "total": 30}
  }
}
```

- `id` is the artist id of `/artist/{id}`; the app sends the ones the user
  picked to `POST /onboarding/complete`.
- The list is ordered by the stored position, which is not exposed. With
  30 artists and the default `limit` of 50 the app receives all of them in
  one page.
- The list is replaced as a whole by a script the repo owner runs
  (`scripts/seed_onboarding_artists.py`). A walk across pages that overlaps
  the moment of a replacement can skip or repeat an artist; start again from
  the first page.

### Image color

Every `image` carries `color`: a lowercase `"#rrggbb"` or `null`. It is
`null` until the server has computed it (the first time it sees the
picture) and a later request carries it; the computation happens after the
response is sent. See [Image tiers](conventions.md#image-tiers) and
[Color](conventions.md#color). Reading the stored colors never fails the
request: on a database failure `color` is `null` and the rest of the body is
the same.

### Headers

Every response, `GET` and `POST`, carries `Cache-Control: private,
no-cache` on success and `no-store` on an error.

## POST /onboarding/complete

Closes the onboarding: saves the artists the user picked as favorites in the
library and marks the profile as onboarded (`onboarding_completed_at` in
`GET /profile/me`). It does not generate recommendations; the app calls
`POST /recommendations/generate` afterwards.

Body:

```json
{"artist_ids": ["UC...", "UC...", "UC...", "UC...", "UC..."]}
```

`artist_ids` has 5 to 20 artist ids, without repeats, each in the format of
the `/artist/{id}` path (`UC...` or `MPLAUC...`, with letters, digits, `_` and
`-` only). They can be ids of `GET /onboarding/artists` or of an artist the
user found in the search. The user is always the one in the token; a
`user_id` (or any other key) in the body is rejected.

| Case | Status | Body |
|---|---|---|
| Completed | 200 | `ok: true`, `data.onboarding_completed_at`, `data.saved`, `data.skipped` |
| Completed again | 200 | the same as the first time: the first timestamp is kept and no artist is duplicated |
| Fewer than 5 ids, more than 20, repeated ids, a malformed id (including characters other than letters, digits, `_` and `-`), a missing or empty `artist_ids`, or another key in the body | 422 | `ok: false`, `reason: "invalid_request"`; nothing is written (see the note below the table) |
| Fewer than 5 of the artists exist after skipping the ones the provider confirms are gone | 422 | `ok: false`, `reason: "too_few_artists"`; nothing is written |
| No profile row for the user | 404 | `ok: false`, `reason: "profile_not_found"`; nothing is written |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Provider failed (not a confirmed missing id) | 502 | `ok: false`, `reason: "upstream_error"`; nothing is written |
| Provider timed out, or the 12 s request budget ran out | 504 | `ok: false`, `reason: "upstream_timeout"`; nothing is written |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

The caller's profile lookup runs before the body is validated. With an invalid
body, a user without a profile gets 404 `profile_not_found`, and a database
failure during that lookup gives 502 or 504 instead of 422.

Shape of a 200:

```json
{
  "ok": true,
  "data": {
    "onboarding_completed_at": "2026-10-30T12:00:00+00:00",
    "saved": 5,
    "skipped": []
  }
}
```

- `onboarding_completed_at` is an ISO 8601 string with offset: the instant of
  the first close.
- `saved` is the number of artists that are in the library after the call
  (new ones and ones the user already had).
- `skipped` are the ids the provider confirmed do not exist, in the order of
  the body. They are not saved. When 5 or more of the others are valid the
  call succeeds; with fewer it is 422 `too_few_artists`.
- Each saved artist is a library item of `kind: "artist"` and `source:
  "external"`, with its name as `title` and its photo as `thumbnail_url`. An
  artist the user already had keeps one row, with the title refreshed and its
  `added_at`. Its stored photo is never overwritten: when the artist arrives
  without a photo (a curated row with `thumbnail_url` null, or a provider page
  without one), the photo it already had is kept, and a new row with no photo
  stores `""`. The rows with a photo and the rows without one are saved in two
  upserts, so the two writes are not atomic with each other; a retry does not
  duplicate.

### How the artists are resolved

1. The ids that are in the curated list are resolved from the database, with
   no provider call.
2. Only the others (the ones from the search) are fetched from the provider,
   with the same page `/artist/{id}` serves (and its server-side cache), 4 at
   a time.
3. An id the provider confirms does not exist is skipped (one `INFO` line in
   the log). Only that confirmation skips an id; any other failure of the
   provider is a 502 or 504 and nothing is written.

### Time

The provider calls happen inside the request, in parallel, 4 at a time,
under the 12 s budget of [Time limits](conventions.md#time-limits). The 4
workers are the module constant `_PARALLEL_CALLS` of
`services/onboarding_service.py` (the measured default of
[ADR 025](../adr/025-recommendations-are-generated-inside-the-request-in-parallel.md)),
not `RECOMMENDATIONS_PARALLEL_CALLS`. That module keeps its own copy of
`_fetch_artist_or_none`, `_skip_if_confirmed_missing`, `_parallel` and
`_in_worker` from `services/recommendations_service.py`: extracting them would
change `recommendations_service` and break the monkeypatch targets of its
tests. Moving them to `core/` is left for a separate issue. Only the
ids outside the curated list cost a call. Measured against the provider with
ids outside the list and nothing cached, three runs each, the call being
`/artist/{id}`'s page (seconds):

| Ids (N) | Total | p50 of a call | Slowest call | Worst case (rounds x slowest call) |
|---|---|---|---|---|
| 5 | 1.09 / 1.11 / 1.35 | 0.764 / 0.815 / 0.884 | 0.791 / 0.865 / 0.899 | 1.58 / 1.73 / 1.80 |
| 10 | 1.38 / 1.85 / 1.51 | 0.405 / 0.418 / 0.397 | 0.824 / 1.42 / 0.900 | 2.47 / 4.26 / 2.70 |
| 20 | 2.42 / 2.14 / 2.18 | 0.346 / 0.322 / 0.304 | 0.914 / 0.778 / 0.916 | 4.57 / 3.89 / 4.58 |

The worst case is the rounds (`ceil(N / 4)`) times the slowest call of the
run. The maximum of 20 ids is kept because its worst case stays under half
of the budget (6 s); the other half covers the probe that each id the
provider says is gone adds, and the overshoot described in ADR 024.

### Writes

The library is written first, in a single statement for all the artists (all
or none), and the profile is marked second. If the second step fails, the
artists stay saved and the profile stays pending; the app retries and the
retry is safe, because saving the same artists again adds no rows and the
profile is only marked if it still has no timestamp. A failure of the first
step marks nothing.

## Database access

The curated list and the profile mark use the service-role client (`get_db`):
`onboarding_artists` has no privileges for the client roles (migration 049),
and `authenticated` cannot update `profiles.onboarding_completed_at`
(migration 050), so the `id` of the token, filtered in the query, is the only
barrier there. The library items are written with the caller's own client
(`get_user_db`), so RLS applies as a second barrier. The `user_id` is always
the one in the token.
