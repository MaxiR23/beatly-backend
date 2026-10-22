# 018. Supabase requests are bounded and GETs retry once

Why both Supabase clients share one transport that bounds each request
and retries a failed GET once, the numbers behind the bounds, and why the
eight read RPCs moved from POST to GET (issue #205).

## Context

Both clients (`get_db`, the service-role one, and the per-user one of ADR
010 and 017) ran with the 120 s per phase that postgrest uses by default
(connect, read, write and pool, each). A connection that dies silently, as
a keep-alive socket dropped by the other side does, therefore held a
request for minutes, and the app's own HTTP client gives up at 15 s
(`http.timeout` of 15000 ms, measured in earlier sessions; it is not in
this repo). The user saw a hang and then an error that the backend never
got to answer.

postgrest does not retry an `httpx.ReadError` or a timeout (ADR 017), and
`HTTPTransport(retries=1)` only retries while connecting. Routes run in
FastAPI's threadpool (40 threads, no wait limit), so a slow upstream also
ties up threads.

## Decision

**Settings.** Six fields in `core/config.py`, all `float`, all `> 0` (a
zero or negative value fails `Settings()` at startup, without echoing the
value), read from the environment in uppercase, no prefix:

| Variable                          | Default | Meaning                                     |
| --------------------------------- | ------- | ------------------------------------------- |
| `SUPABASE_CONNECT_TIMEOUT`        | 2.0 s   | open a connection                           |
| `SUPABASE_READ_TIMEOUT`           | 4.0 s   | wait for bytes of the response              |
| `SUPABASE_WRITE_TIMEOUT`          | 2.0 s   | send the request                            |
| `SUPABASE_POOL_TIMEOUT`           | 2.0 s   | wait for a free connection                  |
| `SUPABASE_REQUEST_BUDGET`         | 10.0 s  | total a request may use before a retry      |
| `SUPABASE_SLOW_REQUEST_THRESHOLD` | 1.0 s   | duration above which a request is logged   |

They are documented in `.env.example`. The sum of the four phases is 10 s
with these defaults (not the maximum duration of an attempt, see "Budget
and clipping"); the code never hardcodes it, it is computed from the
settings.

**One transport.** `core/supabase_transport.py` has `SupabaseTransport`,
which wraps an `httpx.HTTPTransport` and is the only place where the
retry, the budget and the slow log live. `build_supabase_http_client(*,
http2)` builds the `httpx.Client` (timeouts from `Settings`, redirects
followed, that transport) and `core/database.py` injects it into both
clients with `ClientOptions(httpx_client=...)`. `get_db` stays on HTTP/2
and the per-user client on HTTP/1.1 (ADR 017). With an injected transport
httpx ignores the `Client`'s `http2`, `limits` and `verify`, so the
wrapper builds its `HTTPTransport` with `http2` explicit; the default
limits (100 connections, 20 keep-alive) are the same.

**What is retried.** Only `GET`, once, and only on `ConnectError`,
`ConnectTimeout`, `ReadTimeout`, `ReadError` and `RemoteProtocolError`.
Not `PoolTimeout` (no free connection: retrying only waits again), not
`WriteError`/`WriteTimeout` (the request may have been partly sent), not
`ProxyError` or `DecodingError` (the same thing would come back). `HEAD`,
`POST`, `PATCH`, `PUT` and `DELETE` are never retried, including the four
write RPCs (`add_playlist_track`, `add_playlist_tracks_bulk`,
`remove_playlist_track`, `move_playlist_track`), which stay on POST. A
complete HTTP response, 503 and 520 included, is returned as it is: that
retry belongs to postgrest. A second failure is raised as the original
`httpx` exception, so `translate_upstream_errors()` still answers 504
`upstream_timeout` or 502 `upstream_error`; no contract changes. Rejected:
`HTTPTransport(retries=1)` (only on connect) and a retry per service
(many places, easy to miss).

**The body is read inside the attempt.** In httpx 0.28 `handle_request`
returns after the status and headers; the body is read afterwards by
`Client.send`. A `ReadTimeout` or `ReadError` in the middle of the body
would escape a wrapper that only looked at `handle_request`. The transport
reads the body inside each attempt (and closes the response if that read
fails), so the error set above also holds mid-body, and the logged
duration includes the body.

**Budget and clipping.** The retry happens only if
`SUPABASE_REQUEST_BUDGET` minus what the first attempt used is positive.
The second attempt runs with its four timeouts scaled by the same factor,
`min(1, remaining / sum of the four phases)`, set in
`request.extensions["timeout"]` (httpx passes the extension to httpcore,
which reads it per phase). The scaled phases sum to at most `remaining`,
but over HTTPS (`SUPABASE_URL` is https) httpcore applies `connect` twice,
once to the TCP connection and once to the TLS handshake. An attempt
therefore lasts at most `pool + 2*connect + write + read`, 12 s with the
defaults (2 + 2 + 2 + 2 + 4). The scaled second attempt lasts at most
`remaining * 12/10`, so a retried request ends within 12 s at most. The
first attempt is not bounded by the budget, only by its own phases: also at
most 12 s (2 + 2 + 2 + 2 + 4), with no retry after it. That is 3 s under
the app's 15 s. Clipping each phase on its own with `min(phase, remaining)`
was rejected: with a first attempt that used 6 s, the remaining budget is
4 s and the second can still spend min(2, 4) + min(2, 4) + min(4, 4) +
min(2, 4) + min(2, 4), counting connect twice, so 6 + (2 + 2 + 4 + 2 + 2) =
18 s in theory, and (2 + 2 + 4) x 2 = 16 s in the realistic case of a GET
(pool free, instant write: connect twice plus read, twice). The cost of the
proportional scaling is that a retry right after a quick failure runs
with slightly shorter phases than the first attempt did.

**Read RPCs over GET.** A retried request must be a GET, and the eight
read RPC calls went over POST. They now use `get=True`: in
`services/playlist_service.py` `get_playlist_duration_total`,
`get_user_playlist_thumbnails` (two calls), `get_liked_tracks_duration_total`
and `get_owned_playlists_with_track`; in `services/genre_service.py`
`get_playlist_thumbnails` (two calls); in `services/likes_service.py`
`likes_sync_checkpoint`. All six functions are `STABLE` in their current
definition (019, 020, 028, 023, 017 and 038 respectively), so they run in
a read-only transaction. The two `uuid[]` parameters travel in the query
string and PostgREST expects a Postgres array literal there: a python list
renders as repeated parameters and PostgREST rejects it with `22P02`. The
services send `{id1,id2}` built by a private `_uuid_array_literal`; the
ids are uuids already validated. No SQL changes.

**Slow log.** When a request takes longer than
`SUPABASE_SLOW_REQUEST_THRESHOLD`, the transport logs one warning per
logical request, also when it ends in an exception:
`supabase_slow method=%s path=%s duration_ms=%d retried=%s`. Only the
method, the path (never the query string), the total duration of both
attempts and whether it was retried. Never headers, query or body.

## Consequences

- The bounds assume fast DNS and a single resolved address. `getaddrinfo`
  has no timeout and `socket.create_connection` applies `connect` to each
  resolved address, so slow DNS or several unreachable IPs can go past the
  cap.
- Because the transport reads the body inside the attempt, httpx never sets
  `response.elapsed` on either client and reading it raises `RuntimeError`.
  The duration comes from the `supabase_slow` log. A future event hook or
  metric must not use `elapsed`.
- `read` is per read operation, not total: a response that drips bytes
  can go past the budget.
- postgrest sleeps up to 7 s inside its own retry of a 503/520 and can
  send up to four times; each send has its own transport retry. Accepted,
  not disabled.
- A `PoolTimeout` is not retried; it is answered as a 504.
- HTTP/2 on `get_db`: a read error on the shared connection fails all its
  streams together, and all of them retry together on a new connection.
- With a transport injected, httpx stops reading `HTTP_PROXY`,
  `HTTPS_PROXY` and `ALL_PROXY` from the environment for both clients. A
  deploy that reaches Supabase through them needs a check.
- postgrest's `DeprecationWarning` about `timeout` no longer appears: with
  an injected client it is not passed.
- `postgrest.schema(...)` would build a client without this transport; it
  has 0 uses today.
- Slow reads that used to finish (over 4 s of read) now answer 504;
  the slow log is what shows them coming.
- Pending manual check: the ~3.9 KB URL of `GET /playlists` with 100
  playlists against the remote gateway (the local stack accepted it).
- Out of scope and still open: the music provider's own timeout
  (`timeout=30` in its client) and the threadpool queue.
- The tests read private attributes of httpx and httpcore
  (`_transport._inner._pool._http2`); both are pinned in
  `requirements.txt`.
