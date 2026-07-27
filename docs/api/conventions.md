# API conventions

Rules that apply to every endpoint. Per-endpoint documents only
describe what differs from this.

Field-level schemas are in OpenAPI: http://localhost:8000/docs

## Response shape

    {"ok": true, "data": ...}
    {"ok": false, "reason": "..."}

`ok` is always present. `reason` is a stable snake_case identifier
meant to be read by the client, never a message to display.

## Status codes

| Case | Status | reason |
|---|---|---|
| Success | 200 | none |
| Expected empty state | 200 | specific to the case |
| Parent resource does not exist | 404 | `*_not_found` |
| Invalid input | 422 | `invalid_request` |
| Conflict with current state | 409 | specific to the case |
| Not authenticated | 401 | `unauthorized` |
| No permission | 403 | `forbidden` |
| Upstream service failed | 502 | `upstream_error` |
| Upstream timed out | 504 | `upstream_timeout` |
| Unhandled internal error | 500 | `internal_error` |

## The distinction that matters

A 200 with `ok: false` is not an error. The request succeeded and the
answer is that there is nothing. A genre with no playlists returns 200.

A 404 means the parent resource does not exist. A slug that matches no
genre returns 404.

Clients check `ok` in the body, not the status code, to tell "nothing
here" from "navigation error".

## Reasons

| reason | Status | Meaning |
|---|---|---|
| `invalid_request` | 422 | Missing or malformed parameters |
| `unauthorized` | 401 | Missing or invalid token |
| `forbidden` | 403 | Authenticated but not allowed |
| `upstream_error` | 502 | A provider or the database failed |
| `upstream_timeout` | 504 | A provider did not respond in time |
| `internal_error` | 500 | Unhandled error. If you see this, it is a bug |
| `no_genres` | 200 | No genres exist yet |
| `profile_not_found` | 404 | Authenticated user has no profile row |
| `no_library_items` | 200 | Authenticated user's library has no items yet |
| `library_item_not_found` | 404 | No library item matches user_id, kind, external_id |
| `username_taken` | 409 | Requested username already belongs to another profile |