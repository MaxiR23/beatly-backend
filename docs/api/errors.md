## POST /errors/playback

Records an error the app hit while resolving or playing a track. Requires
a token; any authenticated user can report. The report is telemetry: the
client sends it best-effort and does not retry on a failure.

| Case | Status | Body |
|---|---|---|
| Recorded | 200 | `ok: true`, `data: null` |
| Duplicate within 5 seconds | 200 | `ok: true`, `data: null` (nothing is stored) |
| Invalid input | 422 | `ok: false`, `reason: "invalid_request"` |
| Not authenticated | 401 | `ok: false`, `reason: "unauthorized"` |
| Too many reports | 429 | `ok: false`, `reason: "rate_limited"` |
| Database failed | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

Body fields. Lengths are measured after control characters are removed
and the text is trimmed.

| Field | Type | Required | Limits |
|---|---|---|---|
| `track_id` | string | yes | 1-64 characters |
| `platform` | string | yes | `ios` or `android` |
| `os_version` | string | yes | 1-32 characters |
| `app_version` | string | yes | 1-32 characters |
| `stage` | string | yes | `resolve` or `playback` |
| `error_code` | string | yes | 1-64 characters |
| `error_message` | string | yes | 1-1000 characters |
| `http_status` | integer | no | 100-599; stored as `null` when omitted |

`platform` and `stage` are exact: `IOS` or ` ios` is a 422. Any other
field is a 422 too, including `user_id`: the user is always the
authenticated one, taken from the token, never from the body.

Dedup: a report from the same user for the same `track_id` and `stage`
within 5 seconds of the last stored one answers 200 and stores nothing.
It does not count toward the cap.

Cap: a user can have 20 stored reports in any 10 minute window. The 21st
answers 429 `rate_limited` and stores nothing. Under concurrent requests
the cap can be exceeded by a few rows.

The 5 second and 10 minute windows, and the stored `created_at`, are
computed from the API server's clock (UTC), not from the database `now()`.
The dedup and cap queries compare against the same instant that is written
as `created_at`, so the windows are consistent with the rows they count.
See `docs/adr/013-errors-domain-uses-the-service-role-client.md`.

The checks run in this order: validation, dedup, cap, store. The
`track_id` is not looked up in the catalog, so an unknown id is accepted
and there is never a 404.

Text is stored without control characters (line breaks included, they are
removed, not replaced) and trimmed. The response never repeats a value
the client sent. Every response carries `Cache-Control: no-store`.
