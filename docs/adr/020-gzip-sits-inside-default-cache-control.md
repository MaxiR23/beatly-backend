# 020. Gzip sits inside the cache-control middleware

Why the gzip middleware is registered before `default_cache_control`, why the
threshold is 1024 bytes and the level 6, and why the unhandled 500 is never
compressed (issue #209).

## Context

Responses of the list endpoints are large JSON: `GET /playlists?limit=100` is
26032 bytes and `GET /library?limit=100` 34380. Clients on mobile networks
benefit from gzip. Starlette's `GZipMiddleware` does the work, with a default
`minimum_size` of 500 and a `compresslevel` of 9.

The app already has `default_cache_control`, declared with
`@app.middleware("http")`, which makes it a `BaseHTTPMiddleware`. That kind
of middleware forwards the response body to the next layer as a stream of
chunks, not as one message.

## Decision

**Order.** `GZipMiddleware` is registered before `default_cache_control`.
Each registration goes to position 0 of the stack, so the gzip ends up inside
it and receives the whole body in one message. `GZipMiddleware` applies
`minimum_size` only when it gets the body in one message; on a stream it
decides before it knows the size and compresses. Measured with the gzip
registered after (outside): `GET /health`, 38 bytes, went out compressed
(58 bytes). With the order of this record it goes out as is, with no
`Content-Encoding`. `test/test_compression.py` pins the order.

**No `BaseHTTPMiddleware` inside the gzip.** Anything placed inside the gzip
that is a `BaseHTTPMiddleware` would turn the body into a stream again and
bring back the problem above. A new middleware must either be pure ASGI or
go outside the gzip.

**Threshold 1024.** Against Starlette's 500. Below about 1 KB the saving is
nothing: `GET /likes/sync` at 1077 bytes goes to 526 (about half, a small
absolute gain), while the 38 and 42 byte bodies (`/health`, a 404) would grow
to 58 and 62. The threshold is `GZIP_MINIMUM_SIZE`, in `Settings`.

**Level 6.** Against Starlette's 9. Measured on this machine (median of 30
runs of Python `gzip.compress`, `.claude/loop/gzip-before.md` and
`gzip-after.md`):

| body | L6 bytes / ms | L9 bytes / ms |
|---|---|---|
| `GET /playlists?limit=100`, 26032 | 4337 / 0.14 | 4311 / 0.18 |
| `GET /library?limit=100`, 34380 | 4488 / 0.14 | 4450 / 0.16 |
| `GET /album/{id}`, 11117 | 2570 / 0.07 | 2567 / 0.07 |

Level 9 saves 26 to 38 bytes (under 1%) on the large bodies for 14 to 29%
more CPU. Level 1 is faster (0.06 ms on the first row) but 7% larger there
(4649). The level is `GZIP_COMPRESSLEVEL`, in `Settings`.

**The unhandled 500.** The `Exception` handler of `app.py` is served by
Starlette's `ServerErrorMiddleware`, which is outermost, outside the gzip. So
the 500 `internal_error` is never compressed. It is a few dozen bytes, under
the threshold anyway.

## Consequences

- A response of at least the threshold carries `Vary: Accept-Encoding`; a
  shared cache keeps one copy per encoding. `Cache-Control` and its `max-age`
  do not change.
- Status, body and reasons do not change, errors included: a large error is
  compressed like any other body.
- Changing the order of the two registrations, or adding a
  `BaseHTTPMiddleware` inside the gzip, makes `/health` compress again and
  breaks the test.
- The numbers are from one machine and a local server; they justify the
  choice of level, not a CPU budget.
