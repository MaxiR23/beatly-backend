# Auth

Not an endpoint group. `core/auth.py` exposes FastAPI dependencies that
other routers use to authenticate requests and gate them by role. Every
failure goes through the ok/reason contract in `conventions.md`, via the
same domain exceptions as any other endpoint.

## `get_current_user_id`

Resolves the caller's user id from the `Authorization: Bearer <jwt>`
header. The JWT is validated locally (HS256, `SUPABASE_JWT_SECRET`,
audience `authenticated`) in both dev and prod — there is no bypass.

| Case | Status | Body |
|---|---|---|
| Valid token | - | returns the user id (`sub` claim) |
| Missing or malformed `Authorization` header | 401 | `ok: false`, `reason: "unauthorized"` |
| Invalid signature, wrong audience or expired token | 401 | `ok: false`, `reason: "unauthorized"` |

## `get_current_profile`

Depends on `get_current_user_id`, then loads the matching row from the
`profiles` table.

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
