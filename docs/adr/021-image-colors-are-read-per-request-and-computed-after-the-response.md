# 021. Image colors are read per request and computed after the response

Why the color of an image lives in its own table keyed by the image, is read
once per request into a context variable, is computed in a background task
after the response is sent, and which limits adjust it to the dark theme
(issue `color`, built on #207).

## Context

Clients want a color for every image, to tint the screen behind a cover. The
API serves the same image in 27 places that come from five tables and from
the provider directly, in three sizes (ADR 019). Nothing about an image is
stored by the backend today: the tiers are derived when a response is
serialized. A color cannot be derived from a URL, so it has to be computed
from the pixels once, kept, and attached to every response that carries the
image, without making any request wait for a download.

ADR 019 fixed that the computed fields of the models do no I/O. The repo has
no worker and no queue. It has 0 `async def` in `core/`, `routes/` and
`services/`.

## Decision

**A table, not columns.** `public.image_colors` (migration `042`) has one row
per image: `image_key`, `color` (`#rrggbb`, nullable), `attempted_at`
(`NOT NULL`) and `failed_at`. A column per source table would store one color
several times and miss the images that come straight from the provider.
Redis was rejected because the color is immutable data that has to outlive
an eviction and has to be shared by every process.

**The key is the image, not the URL.** `image_key` is the URL up to the first
size suffix `=w<n>-h<n>`, or the whole URL when it has none
(`core/thumbnails.py`). `=w544-h544-p`, `=w192-h192-p` and `=w60-h60` of one
image are one row. Whether the provider really serves one image per prefix is
verified by hand with real data (see Consequences).

**Read once per request, by RPC over POST.** A request notes the image of
every tier it serializes in an `ImageColorScope` held in a `ContextVar`
(`core/image_color_scope.py`, a leaf imported by `core/thumbnails.py`).
After the service returns, the router calls `with_image_colors(colors,
ok_response(value))`: a first `model_dump()` collects the keys, the eligible
ones are read with ONE call to `get_image_colors(p_keys text[])`, and the real
serialization by FastAPI finds the colors in the scope. The computed fields
still do no I/O, and a serialization outside a request (a model test, an
internal `model_dump()`) gives `color: null` because the variable is `None`.
Because the color is resolved on the way out, as the tiers are, a response
cached in Redis does not freeze it.

A single `.in_()` was not viable: PostgREST sends it in the query string of a
GET, `GET /library` with `limit=100` can carry 100 `image` plus 400 `mosaic`
elements = 500 keys, and a key of the provider's CDN is about 140 characters,
so about 70 KB of URL (ADR 018 already leaves a 3.9 KB URL as a pending manual
check). The array goes in the body of a POST. Of the repo's reads, the 8 that
go through an RPC (ADR 018) do it because PostgREST cannot express them as a
select; here the reason is the size of the batch, and for the claim that
`INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING` cannot be sent
through PostgREST at all.

**The dependency is `async def`.** `core/image_color_dependency.py` opens the
scope in `async def image_colors`, the first `async def` in the repo. FastAPI
runs a sync (`def`) dependency in the threadpool on a copy of the context, so
its `ContextVar.set` never reaches the serialization, which runs in the event
loop: with a `def` dependency every color comes out `null` even when it is
loaded. An `async def` dependency is awaited inline, the `set` lives in the
context of the request, and the sync endpoint (threadpool, copied context)
and the serialization see the same scope object. A test
(`test_the_dependency_is_a_coroutine_function`) and the per-endpoint
`carries_the_stored_color` tests fail if someone "fixes" it into a `def`.
Filling the field in each service was rejected for the reasons ADR 019 gives
for the tiers, and post-processing the JSON in a response class was rejected
because it runs in the event loop and its database call would block it.

**A failed read of the table does not fail the request (explicit exception).**
If reading the colors fails (`UpstreamError` or `UpstreamTimeout` from
`translate_upstream_errors()`), the response goes out as it would have, with
`color: null` everywhere, one WARNING (the class name of the failure, never
its message) and no computation for that request. The owner decided this
(B2): a color is decoration, and it is the same degradation as Redis (ADR
006), where `core/cache.py` falls back instead of failing. It is the only
`except` in the request path that turns an upstream failure into a normal
response, and it is limited to this read. Its cost: with the database hung,
every endpoint with images waits up to the `read` timeout of ADR 018 once,
because a POST is not retried. The existing 502 and 504 of each endpoint do
not change.

**A claim in the database, with a retry window.** The computation of a key
starts with `claim_image_colors(p_keys, p_attempted_at, p_retry_before)`: one
statement that inserts a new key, or takes back a key with no color whose last
attempt is older than `image_color_retry_after` (24 h), and returns only the
keys the caller now owns. The primary key guarantees one row; two claims of a
new key at the same time cannot both get it, because the second waits for the
insert, falls into the conflict and its `WHERE` no longer holds. An in-memory
set would not hold with more than one process. A key claimed by a request that
died in the middle waits for the whole window. The dates come from Python, the
clock that stamps `attempted_at` (the same criterion as ADR 013).

**Computed in a background task.** The dependency schedules
`compute_image_colors` as a Starlette `BackgroundTask`, which runs after the
response is sent. There is no worker or queue in the repo, and a script would
only cover what is already stored. A process-wide `BoundedSemaphore` allows
2 computations at once (`image_color_max_concurrency`), so they cannot eat
the 40-thread pool the requests share, and a task that finds no slot gives up
and leaves its keys unclaimed. A request claims at most 10 keys
(`image_color_max_per_request`). Each download is the `small` tier (192 px,
about 8-11 KB) through an `httpx.Client` of its own, with no redirects, a
timeout (`image_color_fetch_timeout`, 3 s, also as a total deadline because
the httpx timeout is per phase) and a cap on bytes (`image_color_max_bytes`,
262144) and on decoded pixels (`image_color_max_pixels`, 2048 x 2048). The
`except` of the background task (a fixed tuple, not `Exception`: `_ImageRejected`,
`httpx.HTTPError`, `httpx.InvalidURL`, `OSError`, `ValueError`,
`Image.DecompressionBombError` and `SyntaxError`, which is how Pillow reports a
broken PNG) is not the antipattern in CLAUDE.md: there is no caller to return
anything to, and the state is persisted in `failed_at`. An exception outside
the tuple is a bug and escapes to the ASGI server log. `_ImageRejected` is a private exception of
the service, not an `AppError`, because no failure of this change reaches a
client or a handler.

**What may be downloaded.** The URLs in a response can be anything a client
stored through `POST /likes`, so the server would otherwise fetch URLs chosen
by a client. A URL is eligible only if it is `https`, on an exact hostname of
`image_color_allowed_hosts` (no wildcards), with no userinfo, port 443 or
none, and at most 2048 characters. A malformed URL, or one with control
characters, is not eligible. IMPORTANT: `IMAGE_COLOR_ALLOWED_HOSTS` must be
set for colors to appear: the list is empty by default, nothing is downloaded
without it and every `color` is `null`; the server logs a WARNING at startup
when it is empty. An ineligible URL is not read, not
registered and not downloaded: recording them as failed would let any client
fill the table with invented URLs. A rejected host is logged at DEBUG, the
hostname only, so the list can be tuned from the logs. Redirects are never
followed.

**The algorithm.** The image is shrunk to 32 x 32 with a box filter
(deterministic), pixels fall in buckets of 4 bits per channel, the most
populated bucket wins (a tie takes the lowest bucket tuple) and the result is
the rounded average of its pixels. Near-black (every channel under 24) and
near-white (every channel over 232) pixels are ignored unless the other pixels
are under 10% of the image. Pillow decodes JPEG, PNG, WEBP and GIF only.

**The color is adjusted to the dark theme.** `color` is not the raw dominant
color. Only the lightness changes (hue and saturation stay), in steps of
0.001, checking each step on the rounded 8-bit RGB:

- If the contrast against the app background is under
  `image_color_min_background_contrast`, it is lightened until it is not.
- Otherwise, if the contrast of white text on top is under
  `image_color_min_white_text_contrast`, it is darkened until it is not.

| Setting | Default | Meaning |
|---|---|---|
| `IMAGE_COLOR_BACKGROUND` | `#0e0e0e` | background of the app, `#rrggbb` lowercase |
| `IMAGE_COLOR_MIN_BACKGROUND_CONTRAST` | 1.5 | WCAG contrast against the background, 1-21 |
| `IMAGE_COLOR_MIN_WHITE_TEXT_CONTRAST` | 4.5 | WCAG contrast of white text on the color, 1-21 |

The owner fixed the criterion for the dark side (stand out from `#0e0e0e`)
but not the number; 1.5 is the default and is configuration: black becomes
`#323232`, 2.0 would give `#454545` and 3.0 `#5f5f5f`. White becomes
`#767676`, pure red `#ee0000`. `Settings` fails at startup if no gray can meet
both limits. Over 20 000 random colors every result met both limits and the
hue drifted at most 3.43 degrees (rounding to 8 bits) for saturations above
0.05.

**The client.** The color lives in `public.image_colors`, which only
`service_role` can touch (RLS enabled with no policy, `ALL` revoked from
`anon` and `authenticated`, as `035` did for a new relation and `038` for a
new function). The routes that serve user data read it with the service-role
client (`get_image_color_db` wraps `get_supabase()`), the same reasoning as
`error_logs` in ADR 013: the data belongs to no user. A separate function
exists so tests can override it without touching `get_db`.

## Consequences

- Every endpoint that returns an image makes one extra RPC when the response
  has an eligible image, including the provider endpoints that were served
  from Redis in milliseconds. A cache of positive colors (they are immutable)
  would remove it and was left out of scope until measured.
- `ImageTiers` is no longer purely derived: `color` is read per request. The
  three URLs are still never stored (correction to ADR 019 in the README).
- `color` is `null` the first time an image is seen and a later request
  carries it. A response a client holds under `Cache-Control: max-age` can keep
  `null` until it expires. A response grows by 13 bytes per tier with `null`
  and 18 with a color (compact JSON, measured).
- A key whose claimer died in the middle waits the whole 24 h window; two
  processes never compute the same key inside it.
- The background task uses the request threadpool; the semaphore (2), the 10
  keys per request and the 3 s deadline bound it to 2 threads x 10 x 3 s in
  the worst case. Computing one 192 x 192 JPEG took about 0.4 ms in a local
  measurement, so the cost is the download, not the decoding.
- Deploy order: apply `042` before the code. Without it the read answers
  `PGRST202`, every response goes out with `color: null` and one warning per
  request, and none fails.
- The routes that serve a user's data now read with the service-role client
  for this one table (correction to ADR 010 in the README).
- Pending manual check with real data: that two sizes of one real image share
  a key, that the CDN answers the `small` tier with `image/*`, no redirect and
  within 3 s, and that the allowed-hosts list is complete (DEBUG log
  `image_color skipped host=`).
