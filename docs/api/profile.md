## GET /profile/me

Returns the authenticated user's profile.

| Case | Status | Body |
|---|---|---|
| Profile exists | 200 | `ok: true`, `data` |
| No profile row for the user | 404 | `ok: false`, `reason: "profile_not_found"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

The profile has `id`, `role`, `username`, `display_name`, `avatar_url`,
`created_at` and `updated_at`. `display_name` and `avatar_url` can be
null. Always scoped to the caller's user id from the auth token.

## PATCH /profile/me

Updates the authenticated user's own profile. Partial update: only the
fields present in the body are changed, any field left out is
unaffected.

| Case | Status | Body |
|---|---|---|
| Profile updated | 200 | `ok: true`, `data` (the updated profile) |
| Username already taken | 409 | `ok: false`, `reason: "username_taken"` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| No profile row for the user | 404 | `ok: false`, `reason: "profile_not_found"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Editable fields: `username` (3-30 chars, `[a-zA-Z0-9_]`), `display_name`,
`avatar_url`. These are the only fields accepted — any other field in
the body (including `role`, `id`, `created_at`, `updated_at`) is
rejected with 422 `invalid_request` before the database is queried. `role`
cannot be changed through this endpoint; it is also enforced at the
database level by a trigger. The request is always scoped to the
caller's user id from the auth token — there is no `user_id` field to
set.
