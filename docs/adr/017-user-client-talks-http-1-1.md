# 017. The per-user Supabase client talks HTTP/1.1

Why `_create_user_client` injects its own HTTP/1.1 `httpx.Client` instead
of using the HTTP/2 one postgrest builds, the measurement behind it, and
why `get_db` stays as it is (issue #203).

## Context

The per-user client is cached by token (ADR 010) and shared by the `def`
routes that run in parallel in the threadpool. postgrest 2.31 builds its
`httpx.Client` with `http2=True`, so parallel requests of one user share a
single connection. httpcore 1.0.9 re-raises the first read error on every
stream of that connection, postgrest does not retry an `httpx.ReadError`,
and `translate_upstream_errors()` turns each one into a 502
`upstream_error`.

Measured with the startup burst (`/profile/me`, `/recents`, `/playlists`,
`/library`, `/likes/sync` in parallel, 300 rounds): with HTTP/2, 5 of 300
rounds had an error, 15 requests in all, always 2 to 4 together, all of
them 502 `upstream_error`. With HTTP/2 off in the per-user client only,
0 of 300.

## Decision

`_create_user_client` builds `httpx.Client(http2=False,
timeout=DEFAULT_POSTGREST_CLIENT_TIMEOUT, follow_redirects=True)` and
passes it to `create_client` as `ClientOptions(httpx_client=...)`. Timeout
and redirects are the ones postgrest uses on its own (120 s, redirects
followed), pinned by hand because postgrest does not apply them to an
injected client, so the protocol is the only observable change. The
behavior is fixed, with no environment variable. The `aclose()` on
eviction is the existing one: `postgrest.session` is the injected client,
so no new closing code is needed.

The `auth` sub-client is built always in `Client.__init__` and uses
`http_client or Client(...)`, so it receives the same injected
`httpx.Client`. This backend does not use it, and eviction closes it
together with postgrest. It is mentioned because it changes what ADR 010
says about the sub-clients (see the correction in the README).

## Consequences

- `get_db` (service-role) stays on HTTP/2 for now, unmeasured. If the same
  symptom shows in the catalog routes, it is another issue.
- Parallel requests of one user open up to one connection per concurrent
  request instead of multiplexing one (httpx limits a client to 100 by
  default). Accepted.
- `postgrest.schema(...)` would go back to HTTP/2: it creates a new
  `SyncPostgrestClient` without the injected `httpx.Client`. There are 0
  uses of it today in `routes/`, `services/` or `core/`.
- No retries on `httpx.ReadError`; out of scope for the issue.
- `h2` stays in `requirements.txt` because `get_db` uses it.
- The protocol test reads a private httpcore attribute
  (`ConnectionPool._http2`); httpx and httpcore are pinned in
  `requirements.txt`.
