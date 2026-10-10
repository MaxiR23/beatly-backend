# 013. The errors domain writes per-user rows with the service-role client

Why `POST /errors/playback` stores a per-user row through the
service-role Supabase client, against the criterion of ADR 010, why every
query is filtered by the token's user id, and why the dedup and cap
windows use the Python clock instead of the database `now()` (#191).

## Context

ADR 010 moved the user-data domains to a client authenticated as the
caller, so the RLS policies enforce ownership at the database as a second
layer. `error_logs` is user-owned data too (it has a `user_id` column),
which by that criterion would put it on `get_user_db`.

But `error_logs` has RLS enabled and no policies at all. The `GRANT ALL`
to `anon`, `authenticated` and `service_role` in `017` does not matter:
with RLS on and no policy, every query from `authenticated` is denied and
only `service_role`, which bypasses RLS, can read or write.

The alternative considered was the caller's JWT with owner-only INSERT and
SELECT policies on `error_logs`, as since #143 for the other user-data
tables. It was rejected for this table: an owner INSERT policy would let
any client holding the public Supabase key and a user token write to
`error_logs` directly through PostgREST, skipping the endpoint's
validation and the per-user cap (dedup 5 s, 20 rows per 10 min). Keeping a
looping client from filling the table is the point of the endpoint, and
the cap only holds if the API is the sole writer.

## Decision

**The errors domain uses `get_db` (service-role), not `get_user_db`.**
It is an exception to ADR 010, which is left as written; this record
documents it. The table stays closed to `authenticated`, so the API is
the only path in.

**Ownership lives in the service layer only here.** Because the
service-role client skips RLS, every query in
`services/error_log_service.py` (the dedup select, the cap count and the
insert) starts from the `user_id` taken from the token, never from the
body. A forgotten filter would mix users, so the integration and mock
tests assert the filter on each query.

**The windows come from the Python clock.** The 5 second dedup window,
the 10 minute cap window and the stored `created_at` are all computed
from one `datetime.now(UTC)` taken per request, not from the database
`now()`. The queries then compare against the very instant written as
`created_at`, so the window and the row it counts agree, and the whole
request uses a single reading of the clock that tests can pin.

## Consequences

- No new RLS policy is added to `error_logs`; the table stays closed to
  `authenticated`.
- There is no database-level second barrier here: per-user isolation
  relies on the service's `user_id` filters, which the mock tests and the
  integration test cover.
- Dedup and cap are not enforced by the database. Under concurrent
  requests the cap can be exceeded by a few rows; the race is accepted.
- Clock skew between API instances can shift a window by that skew; at
  5 s and 10 min it is not material.
- A later move of this domain to the caller's client needs INSERT and
  SELECT policies scoped to `user_id = auth.uid()` first, in a new
  numbered migration.
