# 024. Provider requests are bounded per call and per request

Why every request to the external provider has a connect and a read
timeout, why all the provider calls of one incoming request share a budget,
the numbers behind both, and the worst case of each endpoint (issue #217).

## Context

The provider client was built without a session, so its library falls back
to `timeout=30` on every call. The app's own HTTP client gives up at 15 s
(15 000 ms; it is not in this repo, see ADR 018). A slow provider therefore
held a request and a threadpool thread for up to 30 s per call, long after
the user had seen an error. ADR 018 bounded Supabase and left the provider
open.

Measured on 2026-10-28 from the owner's machine, with the default client,
the same calls and arguments the services use, spaced 3-6 s apart, never in
a burst. 159 calls, 160 HTTP requests, zero errors and zero timeouts.
Each call made one HTTP request, except the first search (two: the GET of
the visitor id).

By call (what an endpoint pays):

| call           |   n | p50 ms | p95 ms | max ms |
|----------------|----:|-------:|-------:|-------:|
| album          |  23 |    283 |    358 |    361 |
| album playlist |  23 |    192 |    289 |    349 |
| artist         |  22 |    366 |    431 |    458 |
| search albums  |  23 |    697 |    809 |    850 |
| search artists |  23 |    606 |    752 |    847 |
| search songs   |  23 |    686 |    988 |   1239 |
| up next        |  22 |    593 |    901 |    992 |
| all            | 159 |    575 |    847 |   1239 |

By HTTP request (what a requests timeout bounds):

| call           |   n | p50 ms | p95 ms | max ms |
|----------------|----:|-------:|-------:|-------:|
| album          |  23 |    278 |    353 |    356 |
| album playlist |  23 |    189 |    282 |    339 |
| artist         |  22 |    355 |    419 |    448 |
| search albums  |  23 |    694 |    801 |    847 |
| search artists |  23 |    606 |    749 |    846 |
| search songs   |  24 |    661 |    791 |   1004 |
| up next        |  22 |    579 |    888 |    980 |
| all            | 160 |    569 |    805 |   1004 |

## Decision

**A session of our own.** `core/provider_session.py` has `ProviderSession`,
a `requests.Session` subclass, and `_provider()` in `core/search_provider.py`
passes one to the client. Its `request()` is the only place where the
timeout, the budget and the slow log live. It assigns `timeout` on
every request (assigned, not defaulted: ours wins over any timeout a future
version of the library passes). The library accepts any
`requests.Session`, and every request it makes, the visitor id GET included,
goes through `request()`.

**Settings.** Four floats in `core/config.py`, all `> 0` (zero or negative
fails `Settings()` at startup without echoing the value), read from the
environment in uppercase, no prefix, documented in `.env.example`:

| Variable                          | Default | Why                                                        |
| --------------------------------- | ------- | ---------------------------------------------------------- |
| `PROVIDER_CONNECT_TIMEOUT`        | 3.05 s  | just above 3 s, the TCP retransmission window              |
| `PROVIDER_READ_TIMEOUT`           | 10 s    | 10x the max per request (1004 ms), 12x its p95 (805 ms)    |
| `PROVIDER_REQUEST_BUDGET`         | 12 s    | 3 s of margin under the app's 15 s                         |
| `PROVIDER_SLOW_REQUEST_THRESHOLD` | 2 s     | above the max per request (1.0 s) and per call (1.24 s)    |

Two validations, with messages that carry no value: `connect + read` below
15 s, and the budget below 15 s. `connect + read` and not only `read`: with
`read < 15` alone, connect 5 and read 14 would let a single call outside any
budget (a script) take 19 s to fail, after the app gave up, which is the bug
of the issue. The threshold is not Supabase's 1 s: the measured max (1004 ms)
would already log.

**A budget per incoming request.** `app.py` registers `ProviderTimeBudget`,
a pure ASGI middleware and the innermost one (it is registered first), that
with `provider_deadline(...)` sets a `ContextVar` holding the instant the
budget ends as the request arrives. Sync routes and dependencies run in the
threadpool, which copies the context, and async ones (such as `image_colors`)
are awaited inline in the same context, so every provider call of the request
sees it. The budget ends at the first of three moments, each one
calling `clear_provider_deadline()`: the last body of the response (or the
`pathsend`) going out through `send`; the `http.disconnect` coming through
`receive`; and the `finally` when the app returns or raises. The
`ContextVar` stores a mutable holder, not the float, and the clear mutates
that holder, so every copy of the request's context shares it. That is the
reason for the holder: with a `StreamingResponse` and `spec_version < 2.4`,
which is what uvicorn 0.51 sends, Starlette sends the body from a child task
while it runs `response.background` in the parent, and a plain `set(None)`
in the child would change only the child's context (measured: the background
saw the spent deadline). With the holder, anything that runs after the
request ended for the client starts with no deadline. This holds by
construction: it does not depend on whoever adds the task remembering
anything.

Why the disconnect is needed: under 2.3 a client that leaves mid-stream
cancels the body, so the last body never goes out, yet Starlette still runs
the background, and the teardown of request-scope dependencies with `yield`,
inside the app, before any `finally` of the middleware. The only message that
passes through the middleware between the disconnect and the background is
the `http.disconnect` of `receive` (measured: without it the background saw
the deadline). Its effect: what is still in flight after a disconnect (a sync
chunk already running in the threadpool cannot be cancelled) loses the total
and keeps `(connect, read)`, so each call stays bounded at 13.05 s. The
`http.request` messages (reading the body) do not clear. The `finally` covers
the tasks that copied the context (`asyncio.create_task`, `asyncio.to_thread`)
and outlive a request that ended without a last body, such as a 500.

A task that wants a
budget arms its own with `provider_deadline(settings.provider_request_budget)`;
without it only `(connect, read)` apply. Outside a request (scripts, tests
without the app) there is no deadline either.

Why pure ASGI and the innermost: a `BaseHTTPMiddleware` runs the app in
another task (`call_next`), so its reset never reached the background tasks.
That is what the first version did, measured: the task saw the spent deadline
and failed without calling. Why clear plus helper and not a fresh budget
automatically: the tasks of one `BackgroundTasks` run in series in the same
context, and the second would inherit what the first left spent.

**Clipping.** Each call uses `(c, r)` with `c = min(connect, R / 2)` and
`r = min(read, R - c)`, R being what is left of the budget, so `c + r <= R`.
Clipping each phase on its own with `min(phase, R)` is not enough: a call
starting with R left could end at `deadline - R + min(connect, R) +
min(read, R)`, whose maximum is `deadline + connect` = 12 + 3.05 = 15.05 s,
over the app's 15 s. Scaling both proportionally as ADR 018 does is not
right either: `connect + read` = 13.05 s exceeds 12 s, so it would always
scale and the first call of every request would get connect = 3.05 x 12 /
13.05 = 2.80 s, under the 3 s that 3.05 exists for. With this rule connect
keeps its 3.05 s while 6.1 s or more are left, and below that the two split
the time. Both stay above zero for any R above zero (urllib3 takes a read
timeout of 0 as already expired). Examples: R = 12 gives (3.05, 8.95), R = 8
gives (3.05, 4.95), R = 4 gives (2.0, 2.0).

**No call with the budget spent.** If the deadline already passed, the call
is not made: `ProviderDeadlineExceeded` is raised, a subclass of
`requests.exceptions.Timeout`. `translate_upstream_errors()` already maps a
`Timeout` to the 504 `upstream_timeout`, so there is no new reason and no
`AppError`: the network layer does not know status codes, and the class is
not in `PROVIDER_ERRORS` (those go to 502).

A call that ends after the deadline returns its response. The deadline is
not checked afterwards, because that would only throw away a complete
response; the next call is not made.

**A response that arrives slowly is not cut.** A read timeout is per socket
operation, and a response that sends a byte just before each expiry never
trips it. Each `recv` stays bounded by `r` (up to 8.95 s on the first call)
but the number of `recv` does not, so that call can run past the budget and
the client's 15 s, and hold a threadpool thread. Shrinking the socket
timeout chunk by chunk does not close it: one read in urllib3 is not one
`recv` (`read1()` can loop several `_raw_read`, and `http.client` reads the
size line and the CRLF of a chunked body with their own loops), so one read
can stretch to several times what is left. Cutting it from another thread
(a `threading.Timer` plus `response.raw.shutdown()`) was tried and removed
before publishing: it depends on how urllib3 2.7.0 returns connections to
the pool, and a race between the timer and the release could send the
`SHUT_RD` to the socket of a concurrent request, possibly another user's.
That is more fragile than the problem. It never showed up in the
measurement: at most 1.0 s per HTTP request, p95 0.805 s, 160 requests. It
is detected with `provider_slow`: such a request logs with a `duration_ms`
above the threshold, and above 12000 if it passed the budget. If it ever
shows in the logs, it is solved in its own issue.

**Slow log.** Per HTTP request, like `supabase_slow`: one WARNING
`provider_slow method=%s path=%s duration_ms=%d` when the duration passes
the threshold, also when the request ends in an exception. The method and
the path without the query string, never the body, the headers or the query,
because the search text and the ids travel there. The probe and the visitor
id GET are logged like any other request (that GET goes to the bare domain,
logged as `/`).

**No retries**, as before (the default adapter has `max_retries=0`). **No
new reason.**

**Why connect 3.05 and read 10 stay as the cap per call.** They bound each
request on its own, with or without a budget: a script, or a budget
configured higher. The budget bounds the sum. With the default budget the
first call of a request runs with (3.05, 8.95): the effective read of
8.95 s is still about 9x the max per request and 11x its p95; the 10 s only
applies with no budget.

**Worst case per endpoint**, calls in series (plus one GET of the visitor id
on the first call of the process):

| Endpoint | Calls | Without budget, (3.05 + 10) x n | With the budget |
|---|---|---|---|
| `GET /search` | 3 | 39.15 s | 12 s + overshoot, unless a response arrives slowly (see above) |
| `GET /album/{id}`, `/public/album/{id}` | 2 + continuations | 26.1 s + 13.05 per continuation | 12 s + overshoot, unless a response arrives slowly (see above) |
| `GET /tracks/{id}/lyrics`, `/related` | 2 (watch + page; or failed watch + probe) | 26.1 s | 12 s + overshoot, unless a response arrives slowly (see above) |
| `GET /tracks/{id}/upnext`, `/credits`, `/public/tracks/{id}` | 1, +1 probe after a failure | 26.1 s | 12 s + overshoot, unless a response arrives slowly (see above) |
| `GET /artist/{id}`, `/public/artist/{id}` | 1 | 13.05 s | min(13.05, 12) + overshoot, unless a response arrives slowly (see above) |

With the measured latency (p95 per request 0.805 s) a typical `/search` is 3
x 0.805 = 2.4 s, far from 12. An album that paginates (`_AUDIO_PLAYLIST_LIMIT
= None`) makes more calls, but they fit in the same budget; the measurement
saw one request per call in 23 of 23.

## Consequences

- The overshoot is not zero. Before and during the response, each socket
  operation is bounded by its clipped phase and the sum by what was left,
  but per operation: `socket.create_connection` applies connect to every
  resolved address, the TLS handshake runs under the connect, and DNS has no
  timeout. A response that arrives slowly has no cap (see above). The calls
  that respect this end near `arrival + 12 s`, and the next one is not made
  if the deadline passed.
- The budget covers only the provider calls. The database (its own budget,
  ADR 018) and what comes after do not count, and a slow database query
  before the provider eats from it. The 3 s of margin is for the threadpool
  queue, the image colors read, serializing, gzip and the network to the
  client.
- Whatever runs after the request ended for the client starts with no
  deadline, see Decision. Today the only background task (the image colors)
  does not call the provider. A code path with its own `ThreadPoolExecutor`
  does not copy the context and runs with `(connect, read)` only. Tasks that
  copied the context (`create_task`, `to_thread`) stop seeing the deadline
  when the request ends. The teardown of dependencies with `yield` that runs
  while the request is still in progress does see the deadline: the one
  declared with `Depends(..., scope="function")`, torn down before the
  response is sent (`fastapi/routing.py` closes the `function_stack` before
  `await response(...)`), and the request-scope ones when the route or the
  stream generator raises. The middleware cannot cover them: they run inside
  `await self.app` before any ASGI message passes. It is also right, because
  the client is still waiting for the error or the rest of the stream. The
  request-scope teardown after a normal response or a cut stream runs with
  no deadline. Today the repo has no dependencies with `yield`.
- A valid but slow response is now cut: a `/search` whose three calls take
  over 4 s each is a 504 where it used to be a late 200 the app no longer
  showed. With the measured maximum (1.0 s per request) it does not happen,
  and `provider_slow` warns if it starts to.
- Out of scope and still open: the threadpool queue (40 threads, no wait
  limit), retries, a cap on the continuations of `get_playlist`, and cutting
  a response that arrives slowly (residual, above).
