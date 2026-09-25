# 010. User-data domains query Supabase as the caller

Why likes, library, playlists, activity, bug reports and profile switch
from the service-role Supabase client to a client authenticated as the
caller's own JWT, why the catalog write inside adding a playlist track
stays on the service-role client, why the per-user client is cached
rather than built on every request, why the cache is a hand-rolled LRU
instead of a new dependency, and what alternatives were rejected (#143).

## Context

Every endpoint in this backend queries Supabase through `get_db`
(`core/database.py`): a single client built once with the
`service_role` key. Up to now, ownership of a row — that a like, a
library item, a playlist, a piece of activity, a bug report or a profile
belongs to one user — has been expressed entirely in this backend's own
`.eq("user_id", ...)` filters, and, for the RPCs that write
`playlist_tracks`, in an explicit caller parameter (`p_added_by`,
`p_user_id`) rather than `auth.uid()`. `017` (the schema baseline) also
carries 32 RLS policies across 16 tables, several of them written to
scope a query to `auth.uid()` — the same ownership, expressed again at
the database, for a client that authenticates as the caller.
`service_role` is not that client: PostgREST's `Prefer: bypassrls`
behavior means a service-role query runs with the `BYPASSRLS` role
attribute, independently of any caller identity, which is why ownership
has lived in the service layer alone so far.

This issue adds the database as a second layer alongside the service's
own filters: the six user-data domains now query Supabase as
`authenticated`, with the caller's own JWT, so the 32 `017` policies
enforce the same `auth.uid()` scoping this backend's filters already
declare, independently, at the database. `031` gives
`cleanup_library_on_playlist_delete()` the same two-layer treatment from
the other direction: that trigger's cleanup is meant to reach every
user's library, by design, so it is made `SECURITY DEFINER` to keep that
reach independent of which caller's `DELETE` fired it.

## Decision

**The six user-data domains — likes, library, playlists, activity, bug
reports, profile — query Supabase as `authenticated`, with the calling
user's own JWT, instead of as `service_role`.** Catalog reads (`genres`,
`public`, and playlists' own catalog upsert, below) are out of scope:
they have no owner column to scope RLS by, and switching their client
buys nothing. Ownership now lives in two layers: the Python-side filters
this backend already wrote, and the 32 `017` policies, which enforce the
same scoping independently, at the database.

**A second Supabase client, `get_user_client(token)` (`core/database.py`),
built with the anon key and authenticated with
`client.postgrest.auth(token)`.** The anon key, not the service-role key,
because PostgREST derives which role a request runs as from the JWT's
own claims once a client is authenticated with `.postgrest.auth()` — the
key selects the unauthenticated baseline, the JWT carries the actual
role. `core/auth.py` gains `AuthenticatedUser` (user id + raw JWT),
`get_current_user` (decodes the JWT once per request), and `get_user_db`
(built from that same decoded JWT) — `get_current_user_id` and
`get_current_profile` are rewired to depend on `get_current_user` too,
so a request decodes its JWT once, not once per dependency.

**The client is cached by JWT, not rebuilt per request.** A `Client`
owns its own `httpx.Client` with its own connection pool; rebuilding one
per request throws that pool away every time and pays TLS/connection
setup again on the next call from the same user. The cache is a
hand-rolled `OrderedDict` + `threading.Lock` LRU — capped at **256**
entries, confirmed by the repo owner — not a new dependency
(`cachetools` or similar): the eviction and locking logic needed here is
small enough that a dependency buys little over roughly a dozen lines,
and the repo avoids adding one without a concrete need to justify it. A
request handler is `def`, not `async def` (FastAPI runs it in AnyIO's
threadpool), which is why the lock is `threading.Lock`, not
`asyncio.Lock`. The entire check-hit-or-create-evict-insert sequence
runs under one lock acquisition, so a cache hit's `move_to_end` and a
cache miss's create-and-possibly-evict can never interleave; building
the client itself is safe to do under the lock because `Client.create`
does no network I/O (`supabase/_sync/client.py`) — it only reads a
session out of in-memory storage. An evicted client is closed with
`evicted.postgrest.aclose()` (verified against the installed
`postgrest==2.31.0`: `SyncPostgrestClient.aclose` is a synchronous
method despite the name — it calls `self.session.close()`) so the
`httpx.Client` connection pool behind it, and the file descriptors it
holds, are released rather than leaked. Nothing besides `postgrest` is
closed: `auth`, `storage` and `functions` sub-clients are lazily
constructed and none of them is ever touched by this backend, so none of
them ever opens a socket to close (verified against
`supabase/_sync/client.py` and
`supabase_auth/_sync/gotrue_base_api.py`).

**The catalog write inside `add_track`/`add_tracks`
(`services/playlist_service.py`) stays on the service-role client.**
`public.tracks` has no RLS policy granting `authenticated` a write, and
adding one is out of scope for this issue (see Alternatives below) — so
`add_track` and `add_tracks` take a second client parameter,
`catalog_db`, used for exactly one call each (`_upsert_tracks`), while
every other read and write in those two functions runs on the caller's
own client. `routes/playlists.py` wires this by injecting `catalog_db:
Client = Depends(get_db)` alongside `db: Client = Depends(get_user_db)`
on the two add-track routes only.

**Three RLS policies `017` never needed are added, in `030`.** With
`service_role`, `play_events` and `recent_activity` never needed an
INSERT (or, for `recent_activity`'s upsert, UPDATE) policy, and
`bug_reports`' INSERT policy could stay scoped to
`is_tester_or_higher()` because nothing ever evaluated it. Under the
user-scoped client all three become load-bearing: `030` adds INSERT on
`play_events`, INSERT and UPDATE on `recent_activity`, both scoped
`user_id = auth.uid()`, and widens `bug_reports`' INSERT policy to any
`authenticated` caller inserting their own report — matching
`routes/bug_reports.py`, which never gated `POST /bug-reports` by role.
No gate is added to that endpoint to match the old policy instead; see
Alternatives.

**`cleanup_library_on_playlist_delete()` becomes `SECURITY DEFINER`, in
`031`.** The cleanup this trigger runs is meant to reach every saver's
`library_items` row for a deleted playlist, not just the deleting user's
own — that is the whole point of the trigger, and how it behaved under
`service_role`, which runs `DELETE FROM library_items` (`SECURITY
INVOKER` in `017`) unfiltered by RLS. Under the user-scoped client the
same statement is filtered by `library_items deletable by owner`, which
scopes it to the deleting user's own row. `031` keeps the cleanup's
intended, all-users reach independent of who triggered the `DELETE` by
making the function run as its owner, the same pattern `018` and `021`
already established for every other `SECURITY DEFINER` function in this
schema: a pinned `search_path` and `EXECUTE` revoked from
`PUBLIC`/`anon`.

**Alternatives rejected:**

- *A write policy on `public.tracks` for `authenticated`.* Would let the
  catalog upsert run on the same client as the rest of `add_track`, but
  `tracks` is a shared catalog with no owner column — a blanket write
  policy protects nothing an RLS policy is meant to protect, and is a
  separate, larger design question than this issue's scope.
- *Gating `POST /bug-reports` to testers and above, matching the table's
  pre-existing policy instead of widening it.* Rejected by the repo
  owner: the endpoint's documented contract is that any authenticated
  user can submit a report, and `030` is written to match that contract,
  not the other way around. Restricting who can report is a change to
  that contract, and a candidate for its own issue.
- *`cachetools` (or another off-the-shelf LRU) for the per-user client
  cache.* The hand-rolled `OrderedDict` + lock is small, and this issue's
  locking requirement — evict-and-close under the same lock as
  insert — is specific enough that a generic cache library would need
  wrapping anyway.
- *Closing the `auth`/`storage`/`functions` sub-clients on eviction,
  alongside `postgrest`.* None of the three is ever used by this
  backend and none opens a connection lazily-constructed clients don't
  need, so closing them is dead code with nothing to verify it against.

## Consequences

- 30 of the 36 `Depends(get_db)` sites across the six user-data domains'
  routers switch to `Depends(get_user_db)`; `genres` (4 sites) and
  `public` (2 sites) are unchanged, and `album`, `artist`, `tracks` and
  `search` never depended on either.
- `add_track` and `add_tracks` are the only two service functions with
  two client parameters; every other function in
  `services/playlist_service.py` and the other five domains' services
  keeps its existing single-`db` signature.
- `030` and `031` are applied, in that order, before this code is
  deployed: the user-scoped client's writes depend on `030`'s policies,
  and playlist deletion's library cleanup depends on `031`.
- `030` drops `bug_reports`' only policy that called
  `public.is_tester_or_higher()`; after it, the function has no caller
  left at all, in a policy or in any function body. It is kept, not
  dropped — removing an orphaned helper is a separate, out-of-scope
  change (`db/migrations/030_owner_write_policies_for_user_client.sql`,
  its `db/migrations/README.md` entry).
- **Design trade-off, accepted:** the eviction path closes an evicted
  client (`evicted.postgrest.aclose()`) under the same cache lock as the
  insert that evicted it, rather than tracking whether another request is
  still using that client and deferring the close until it is not. The
  256-entry cache size was chosen well past FastAPI's default 40-thread
  AnyIO pool specifically to make that window unlikely in normal
  operation. Reference-counting cached clients (or otherwise deferring
  the close) would close that window for good, at the cost of the extra
  bookkeeping it needs; that is a follow-up for a separate issue, not
  this one.
- `db/migrations/README.md`'s entries for `019` and `020` gain a note:
  the RPCs those files version are called by the user-scoped client on
  the authenticated playlist routes now, not `service_role`; both stay
  `SECURITY INVOKER` and return the same rows either way, because their
  own `SELECT` policies already cover what each RPC's explicit parameter
  scoped. Neither file is edited — the note lives in the README, per this
  repo's rule that an applied migration's obsolete prose is corrected
  there, not in the file.
