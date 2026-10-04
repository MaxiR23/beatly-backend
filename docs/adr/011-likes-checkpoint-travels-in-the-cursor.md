# 011. The likes sync checkpoint travels inside the pagination cursor

Why `GET /likes` and `GET /likes/sync` return the same `data.checkpoint`
on every page of a read by carrying it inside the opaque pagination
cursor, as an optional claim that only a `SortKey` which declares it
accepts, and which alternatives were rejected (#180).

## Context

A client that syncs likes needs a value to send as `since` on its next
`GET /likes/sync`. Computing it from the newest `updated_at` it received
loses rows that were written just before the read and committed after
it. #180 moves that safety margin to the server: the database clock at
the start of the read, minus 60 seconds (`likes_sync_checkpoint()`,
migration `038`), returned as `data.checkpoint`.

The contract is that every page of one read returns exactly the same
value, the empty page included. But the cursor in `core/pagination.py`
is opaque and stateless: the server keeps nothing between pages, and
`_cursor_from_payload()` rejects any payload whose keys are not exactly
`{"k", "i", "s"}`. For pages after the first to return the first page's
value, it has to travel with the client in some form. The helper is
shared by several domains, so whatever is chosen must not change their
cursors.

## Decision

**The checkpoint is an optional fourth claim, `"c"`, of the cursor.** It
exists only for a `SortKey` that declares `carries_checkpoint=True`
(likes). For those sorts the cursor requires exactly `{"k", "i", "s",
"c"}`; for every other sort it still requires exactly `{"k", "i", "s"}`.
The first page reads the clock through the RPC, before the data query,
and passes the value to `build_page`, which encodes it into
`next_cursor`; later pages read it from the decoded cursor and call no
RPC. The value is an opaque string emitted as the database returned it,
and the cursor carries it without reformatting, so all pages return the
same bytes. Unlike `"k"` and `"i"`, the claim is never sent to PostgREST,
only echoed to the client. It is validated as an ISO-8601 string with a
time zone by one function, `is_checkpoint`, used at decode (422
`invalid_cursor`), at emission in `build_page` (`ValueError`, an
invariant of the code) and on the RPC response (502, an upstream
anomaly). The response shape is `LikesPage` (`items`, `page`,
`checkpoint`); `Paginated` and `PageBlock` do not change.

**Alternatives rejected:**

- *An extra query parameter, for example `checkpoint=...`, sent along
  with `cursor`.* Breaks the opacity of the cursor: the client would have
  to know it must copy a second value on every page, and the server would
  have to trust a client-supplied value that the cursor was meant to
  protect. It also adds a request shape that two routes would have to
  validate against the cursor.
- *Returning the checkpoint only on the first page.* Contradicts the
  contract that every page returns it, and pushes onto the client the
  job of remembering it from page one; a client that resumes from a
  later cursor, or whose first page was lost, would have none.
- *Accepting legacy cursors (without `"c"`) and filling in a fresh or
  null checkpoint.* A fresh value would differ from the first page's, so
  "every page returns the same value" would stop being true for exactly
  the reads in flight at deploy, and a later page's clock reading is
  later than the data it already walked, which is the unsafe side. A
  null would make the field optional for the client, who would then have
  to handle two shapes. It also needs new decode code for a transitional
  case.

## Consequences

- A likes `cursor` issued before the deploy has no `"c"`: both routes
  answer 422 `invalid_cursor`, the same status and reason as any invalid
  or expired cursor, and the client restarts the sweep from its first
  page. It needs no new reason and no code beyond the existing key check.
- The cursors of every other domain are byte-identical to before: the
  claim is a field with a default in `SortKey` and a keyword argument
  with a default in `build_page` and `encode_cursor`, and a sort that does
  not declare it keeps rejecting `"c"`. A helper test pins the exact
  encoding of a cursor without checkpoint.
- The first page of each read pays one RPC before the data query; the
  following pages pay none.
- A cursor is slightly longer for likes, by the size of the base64 of
  the timestamp.
- `038` is applied before the code is deployed: without the function
  both GET routes of likes answer 502.
- The checkpoint serves to start a later sync, not to continue its own
  walk: `GET /likes` pages by cursor, and `since` is not modified by the
  server (`docs/api/likes.md`, Checkpoint).
