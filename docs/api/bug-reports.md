## POST /bug-reports

Creates a bug report for the authenticated user. Any authenticated user
can create a report — there is no minimum role, unlike `GET
/bug-reports` and `PATCH /bug-reports/{report_id}` below, which require
admin.

| Case | Status | Body |
|---|---|---|
| Created | 200 | `ok: true`, `data` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`reporter_id` is always the authenticated user's id from the token,
never a client-supplied value, and `status` always starts `"open"`.
Neither can be set by the request body; both are rejected as unknown
fields (422) if sent.

`category` is one of `playback`, `loading`, `ui`, `crash`, `other`.
`description` must be 5-2000 characters. `entity_type`, if sent, is
one of `track`, `album`, `artist`, `playlist`. `entity_type` and
`entity_id` must be sent together or not at all — sending only one of
the pair is a 422 `invalid_request`, not a database constraint
violation.

## GET /bug-reports/me

Lists the authenticated user's own bug reports, newest first,
cursor-paginated. See the Pagination section of `conventions.md` for the
shared `limit`/`cursor` query params and the `data.items`/`data.page`
shape.

| Case | Status | Body |
|---|---|---|
| Has reports | 200 | `ok: true`, `data.items`, `data.page` |
| Has no reports | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"bug_reports": [...]}` and an
empty result used to be `ok: false, reason: "no_bug_reports"`. Both are
gone: an empty result is now a normal empty first page (`ok: true`), per
the Pagination section of `conventions.md`. `no_bug_reports` is
deprecated entirely — no endpoint returns it anymore.

Scoped to the authenticated user — every query filters on
`reporter_id`, including the ones fetched via `cursor`, not only the
first page.

There is no `sort` or `order`: the order is fixed, `created_at`
descending, with `id` breaking ties on reports created at the same
instant. `GET /bug-reports/me` and `GET /bug-reports` share the same
sort key, so a cursor emitted by one is accepted by the other — always
scoped according to the endpoint that receives it.

## GET /bug-reports

Lists all bug reports, from every user, newest first, cursor-paginated.
Requires an admin role.

| Case | Status | Body |
|---|---|---|
| Reports exist | 200 | `ok: true`, `data.items`, `data.page` |
| No reports exist | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Authenticated, not admin | 403 | `ok: false`, `reason: "forbidden"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

**Breaking change:** `data` used to be `{"bug_reports": [...]}` and an
empty result used to be `ok: false, reason: "no_bug_reports"`. Both are
gone: an empty result is now a normal empty first page (`ok: true`), per
the Pagination section of `conventions.md`. `no_bug_reports` is
deprecated entirely — no endpoint returns it anymore.

Unlike `GET /bug-reports/me`, this endpoint is **not** scoped to the
caller — it returns reports from every reporter, by design, so admins
can triage across users.

Each item also carries `reporter`, an object with the `id`,
`display_name` and `username` of the profile of whoever filed the report
(not the admin making the call). `reporter.id` is always equal to
`reporter_id`, which is kept. `display_name` and `username` can be null
when the profile has none set. If the reporter's profile is not visible,
the item carries `reporter.id` with both names null and the page still
answers 200. The reporter comes from the same query as the page.

## PATCH /bug-reports/{report_id}

Updates a bug report's status. Requires an admin role.

| Case | Status | Body |
|---|---|---|
| Updated | 200 | `ok: true`, `data` |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Authenticated, not admin | 403 | `ok: false`, `reason: "forbidden"` |
| Unknown id | 404 | `ok: false`, `reason: "report_not_found"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

`status` is one of `open`, `closed`. This is the only field the
request body accepts; category, description and the entity pair are
set at creation and not editable here.

## Fields

All four endpoints return, for each bug report, `id`, `reporter_id`,
`category`, `description`, `entity_type`, `entity_id`, `status`,
`created_at`, `updated_at`.
`entity_type` and `entity_id` can be null (they're either both present
or both absent).

`reporter` is exclusive to `GET /bug-reports`; `POST /bug-reports`,
`GET /bug-reports/me` and `PATCH /bug-reports/{report_id}` do not
return it. Its fields are `id` (string, the reporter's user id),
`display_name` (string or null) and `username` (string or null).

## Database access

Every query on this page runs on `get_user_db` (`core/auth.py`): the
caller's own JWT, not the service-role client. `GET /bug-reports` is
deliberately unscoped in Python (see above) and relies on the caller's
admin role, checked by `require_role(Role.ADMIN)` before the query runs,
not on an RLS policy scoping it — Supabase RLS still applies as a second
barrier on every query here, behind whatever explicit filter
`services/bug_report_service.py` sets, including none.

### The `reporter` embed in `GET /bug-reports`

If the embed returns a null `reporter` for a report, the item degrades
to `{id: reporter_id, display_name: null, username: null}` and a warning
is logged; the page does not fail with a 502. This is on purpose: a
reporter without a visible profile must never fail the admin page. It
departs from the likes embed, where a null relation is treated as a
502.

The completeness of `reporter` depends on the profiles SELECT policy
"Developers and admins can view all profiles" (created in 017, scoped
`TO authenticated` in 025). If that policy changes, the page does not
fail: it degrades to null names, visible only through the warning in the
server log.
