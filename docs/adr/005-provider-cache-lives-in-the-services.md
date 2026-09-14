# 005. The provider cache lives in the services, one key per operation

Why a Redis cache for the external provider's responses is wired into
each service function instead of into `core/search_provider.py`, why
every 200 (including the documented empty ones) is cached and no
`core/exceptions.py` exception ever is, why the cache read happens
outside `translate_upstream_errors()`, why only `search` hashes its
key, why a Redis failure is invisible to the client, and where the
count of eight cached operations comes from.

## Context

The issue that motivated this record asked for a cache in front of
the external provider, "the five domains", with a table of seven TTL
values (`album`, `lyrics`, `credits`, `artist`, `related`, `upnext`,
`search`). Both numbers undercounted. The "five" in the issue's own
body counted **modules**, not operations: five files under
`services/` mention `SearchProvider` (`album_service.py`,
`artist_service.py`, `search_service.py`, `track_service.py`,
`public_service.py`), and five routers inject
`Depends(get_search_provider)` (album, artist, search, tracks,
public) — either path lands on five, and neither counts a call to the
provider. The table of seven TTLs counted the seven endpoints that
live in a domain router (1 album + 1 artist + 1 search + 4 tracks)
and missed the three endpoints of `routes/public.py`: two of them
(`/public/album`, `/public/artist`) reuse an operation the table
already had, which is why they went unnoticed, and the third,
`GET /public/tracks/{track_id}`, calls
`services/track_service.py::get_track()` — a fifth function in that
file, independent of the other four, that the table's seven never
named. The count by operation of the provider, the one this cache
keys on, is **eight**: `album`, `artist`, `search`, `upnext`,
`lyrics`, `related`, `credits`, `track`.

`services/track_service.py::get_track_lyrics()` and
`get_track_related()` both call `_watch_playlist()` first (the same
`provider_get_watch_playlist` that `get_track_upnext()` and
`get_track()` use) and then their own second call. `get_track_credits()`
never calls `_watch_playlist()`: it builds its own `"MPTC"`-prefixed
browse id and calls `provider_get_song_credits` on its own. So four of
the eight operations (`upnext`, `lyrics`, `related`, `track`) share one
sub-call to the provider, each under its own key and its own TTL.

`services/track_service.py::get_track_lyrics()` returns
`TrackLyricsResult()` (200, `lyrics: null`) in two legitimate branches
(no lyrics tab, or the provider answers with nothing), and
`get_track_credits()` returns `TrackCredits()` (200, every typed
section null) when ADR 004's probe confirms the track exists but the
provider has no credits dialog for it. Both are verified 200s, not
failures.

A value already in Redis is JSON built by `model_dump_json()` on one
of the eight response models. If a later change to one of those
models makes an old cached value fail `model_validate_json()`, that
raises `pydantic.ValidationError` — the same exception
`core/upstream.py::translate_upstream_errors()` already translates to
502 `upstream_error` when it comes from the provider's own response.

## Decision

**The cache lives in the services, one key per operation, not inside
`core/search_provider.py`.** Each of the eight functions wraps a
`_fetch_*` (the untouched original body, moved without editing a
line inside it) with the same four-line skeleton: build a key, read
the cache, return on a hit, otherwise call `_fetch_*` and write its
result with the operation's own TTL. The alternative — caching inside
`core/search_provider.py`, at the level of `provider_get_watch_playlist`
itself — would let `upnext`/`lyrics`/`related`/`track` share one cache
entry for free, but that entry could carry only one TTL, and the
provider client has no way to know which of the four domains is
calling it. The four TTLs this issue requires (6h/24h/12h/24h) have no
natural home there. The accepted cost of the services-first design is
that a miss of any one of the four still pays for its own call to
`_watch_playlist()`, even when another of the four is already cached
for the same `track_id`. `core/search_provider.py` does not import
`core/cache.py` and does not know a cache exists.

**Every 200 is cached, including the documented empty ones; no
exception from `core/exceptions.py` ever is.** `TrackLyricsResult()`
and `TrackCredits()` in their empty shapes are cached with the normal
TTL of their domain, the same as a full response: they are correct
answers the provider already confirmed, not something to be
retried on every request in case it turns non-empty. `NotFound`,
`UpstreamError` and `UpstreamTimeout` are raised by `_fetch_*` before
its wrapper ever reaches the `cache_set` call — the write happens
strictly after the fetch returns a value, so a raised exception is
never seen by the cache. This needs no extra branching: the exception
propagates straight out of `get_*`, past the point where `cache_set`
lives.

**`cache_get` runs outside `translate_upstream_errors()`.** A stale
cached value that raises `ValidationError` on
`model_validate_json()` is a cache failure, not a provider failure: it
must be treated exactly like a miss, logged as a failure, and fall
through to the provider — never surfaced as 502. Since
`translate_upstream_errors()` already turns `ValidationError` into
`UpstreamError` (`core/upstream.py`), the read has to sit outside that
block on purpose. `core/upstream.py` is not modified by this issue:
its `ValidationError` handling is exactly what already protects a
malformed provider response, and inventing a second meaning for the
same exception inside the same block would make the two
indistinguishable.

**Only `search`'s key is normalized, truncated and hashed; the other
seven use the provider's own id as-is.** `q` is free text a person
types into a search box, with no format, no charset and no length
limit enforced anywhere before it reaches
`services/search_service.py`. Its key is built by lowercasing,
trimming, collapsing internal whitespace (`" ".join(text.lower().split())`,
which also folds tabs and newlines), cutting to 200 characters, and
taking the `sha256` hex digest of the result — so `"Beatles"` and
`"beatles "` share one entry, no query reaches a Redis key name raw,
and every search key has the same fixed length. The other seven
operations are keyed by `album_id`, `artist_id` or `track_id`: opaque
ids issued by the provider, never typed by a person, with no
normalization question to answer.

**A Redis failure, on read or on write, is invisible to the HTTP
client and produces no new `reason`.** `core/cache.py::cache_get`
catches `(RedisError, ValidationError)`; `cache_set` catches
`RedisError`. Both log one `WARNING` line with the key and
`type(exc).__name__`, never the exception's message and never
`logger.exception` — the same shape
`core/search_provider.py`'s one caught-failure log already uses, so a
Redis outage does not flood the log with a stack trace per request
while still leaving a queryable signal. `cache_get` returning `None`
on a caught failure is not the antipattern `CLAUDE.md` prohibits
(swallowing an error to invent an empty value): it is exactly what a
miss returns, so the caller falls through to the provider and answers
precisely what it would have answered with no cache at all. A cache
miss logs at `DEBUG` with a different message
(`"cache miss key=%s"`), so a miss and a failure are never the same
call to the logger — the one property this record exists to fix is
that a person reading the log, or a test inspecting a mocked logger,
can always tell the two apart. This invisibility is specifically about
**Redis being down or unreachable at request time**: `core/config.py`
validates that `redis_url` carries a `redis://`, `rediss://` or
`unix://` scheme and raises `ValidationError` if it does not, so a
malformed `REDIS_URL` is a configuration error that fails `Settings()`
at startup, on purpose, rather than a runtime failure that degrades
silently — the two are different failure modes with different intended
visibility.

**The three endpoints of `routes/public.py` that talk to the provider
share their cache entry with their token-bearing equivalent, on
purpose.** `/public/album/{id}` and `/public/artist/{id}` call
`get_album()`/`get_artist()` exactly as `/album/{id}` and
`/artist/{id}` do; `/public/tracks/{track_id}` calls `get_track()`. No
namespace or prefix separates the public and the authenticated path:
they are the same provider operation on the same public data, and none
of the three receives a `user_id` (verified: `get_album`, `get_artist`
and `get_track` take no `user_id` parameter, the same property
`routes/album.py`, `routes/artist.py` and `routes/tracks.py` already
document with "Access policy only"), so there is no user data to leak
across the boundary. A separate `public:` prefix would only buy paying
the provider twice for the same answer the day an authenticated
`GET /tracks/{track_id}` exists.

TTL by operation:

| Operation | TTL | Seconds |
|---|---|---|
| `album` | 24h | 86400 |
| `track` | 24h | 86400 |
| `lyrics` | 24h | 86400 |
| `credits` | 24h | 86400 |
| `artist` | 12h | 43200 |
| `related` | 12h | 43200 |
| `upnext` | 6h | 21600 |
| `search` | 1h | 3600 |

`track` (`services/track_service.py::get_track()`, serving
`GET /public/tracks/{track_id}`) is aligned with the three other 24h
operations, not with `upnext`'s 6h, even though it shares `upnext`'s
own `_watch_playlist()` sub-call: `get_track()` maps only the queue's
first item to `TrackRef` (title, artists, duration, album), stable
metadata, never the queue itself. `upnext`'s 6h exists because the
*queue* is what changes; that reason does not apply to a single
track's own metadata.

## Consequences

- **A miss of `lyrics`, `related` or `track` still pays for its own
  call to `_watch_playlist()`** even when `upnext` for the same
  `track_id` is already cached, and vice versa. Deduplicating that
  sub-call would need a second cache layer inside
  `core/search_provider.py`, rejected above.
- **A future change to one of the eight response models produces one
  `WARNING` per request** against every entry cached under the old
  shape, until each entry's TTL expires. The `v1` segment of the key
  prefix (`beatly:v1:...`) is the lever: bumping it to `v2` in the
  same deploy invalidates every entry at once, cheaper than waiting
  out the TTLs.
- **More surface is servable from cache with no token behind it.**
  The three `routes/public.py` endpoints that reach the provider are
  entirely cacheable, which is the point: a shared link that goes
  viral now costs the provider one call per TTL window instead of one
  call per request. Accepted because none of the three operations
  carries `user_id`.
- **Frescura, not correctness, is what a client gives up.** An album,
  artist, track or search result the provider corrects can keep
  answering with the old value for up to its TTL (up to 24h), with no
  invalidation endpoint, no invalidation command, and no invalidation
  on write — the TTL is the only policy this issue ships. Deleting a
  key by hand with `redis-cli` is the only lever until an
  invalidation mechanism is a separate issue.
- **A `socket_timeout`/`socket_connect_timeout` of 0.25s bounds each
  attempt against a hanging Redis**, but redis-py's own retry-on-error
  behavior means the worst case is a multiple of that, not a hard
  0.25s ceiling. Measuring the real number against a frozen Redis
  container is manual QA for this issue; if it turns out to be
  seconds rather than sub-second, an explicit `retry` policy is the
  next lever, not a rewrite of this design.
- **No `maxmemory` or eviction policy is set on the local
  `docker-compose.yml` Redis.** Every key here carries a TTL, so it
  self-cleans; a production Redis with no eviction policy configured
  could still fill up, but that is deployment configuration, out of
  scope for this record.
