# Auth

Not an endpoint group. `core/auth.py` exposes FastAPI dependencies that
other routers use to authenticate requests and gate them by role. Every
failure goes through the ok/reason contract in `conventions.md`, via the
same domain exceptions as any other endpoint.

## `get_current_user`

Resolves the caller's user id and raw JWT from the `Authorization: Bearer
<jwt>` header. The JWT is validated locally (HS256, `SUPABASE_JWT_SECRET`,
audience `authenticated`) in both dev and prod — there is no bypass.

| Case | Status | Body |
|---|---|---|
| Valid token | - | returns an `AuthenticatedUser` (`user_id` from the `sub` claim, `token` the raw JWT) |
| Missing or malformed `Authorization` header | 401 | `ok: false`, `reason: "unauthorized"` |
| Invalid signature, wrong audience or expired token | 401 | `ok: false`, `reason: "unauthorized"` |

## `get_current_user_id`

Depends on `get_current_user` and returns its `user_id`. Same cases as
`get_current_user` above — this is the dependency most routes take when
they need only the id, not the token.

## `get_user_db`

Depends on `get_current_user` and returns a Supabase client authenticated
as the caller: the anon key plus the caller's own JWT
(`client.postgrest.auth(token)`), so every query it runs is subject to
Supabase RLS as that user, not as `service_role`. Clients are cached by
JWT in a bounded LRU (`core/database.py`) so a request does not build a
new one on every call; the same 401 cases as `get_current_user` apply
before a client is ever built. A database failure on a query made with
this client surfaces the same way as on any other: 502 `upstream_error`
or 504 `upstream_timeout` via `translate_upstream_errors()`. Used by the
six user-data domains — likes, library, playlists, activity, bug
reports, profile — in place of the catalog's `get_db`; see each domain's
own page for its "Database access" note.

## `get_current_profile`

Depends on `get_current_user_id` for the id and `get_user_db` for the
client, then loads the matching row from the `profiles` table with the
caller's own JWT.

| Case | Status | Body |
|---|---|---|
| Profile exists | - | returns the `Profile` (`id`, `role`) |
| No profile row for the authenticated user | 404 | `ok: false`, `reason: "profile_not_found"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

A `profile_not_found` here means the JWT is valid but the user has no
profile row yet — distinct from `unauthorized`, same distinction the
rest of the contract draws between "no permission" and "nothing here."

## `require_role(role)`

Depends on `get_current_profile`. Roles are ranked, lowest to highest:
`user < tester < developer < admin`. A profile satisfies the dependency
if its rank is at or above the required role's — e.g. an admin passes
`require_role(Role.TESTER)`.

| Case | Status | Body |
|---|---|---|
| Profile's role rank >= required | - | returns the `Profile` |
| Profile's role rank < required | 403 | `ok: false`, `reason: "forbidden"` |

Use as `Depends(require_role(Role.ADMIN))` on a route.
