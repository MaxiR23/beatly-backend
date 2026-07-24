# GET /genres

Lists all genres, ordered by `sort_order`.

See `routes/genres.py` and `services/genre_service.py`. Field-level
schema is in OpenAPI (`/docs`), not here.

| Case                          | Status | Body |
| ------------------------------ | ------ | ---- |
| Genres exist                  | 200    | `ok: true`, `data.genres` |
| Table is empty                | 200    | `ok: false`, `reason: "no_genres"` |
| Supabase call fails            | 502    | `ok: false`, `reason: "upstream_error"` |
| Supabase call times out        | 504    | `ok: false`, `reason: "upstream_timeout"` |
| Supabase returns a malformed row | 502  | `ok: false`, `reason: "upstream_error"` |

An empty table is not an error: it is the expected empty state, so it
stays 200 per the response contract in `AGENTS.md`.
