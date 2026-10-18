# 014. The bug reports cursor is bound to the status filter through SortKey.scope

Why a `GET /bug-reports` cursor issued under one `status` filter is
rejected under another, by an optional `scope` on `SortKey` that enters
the cursor tag only when set, which alternatives were rejected, and why
the 422 on a repeated `status` is local to that endpoint (#197).

## Context

`GET /bug-reports` gains an optional `status` filter (`open` or
`closed`). The cursor in `core/pagination.py` is keyset: it names the
last row of a page, not the query that produced it. A cursor issued
under `status=open` and sent with `status=closed` (or with no filter)
would decode, and would start the page after a row that the new query may
not even contain, so the client would silently skip or repeat rows. The
contract is that such a cursor is 422 `invalid_cursor`, like any other
cursor that does not belong to the request.

Two constraints shape the choice. The helper is shared by several
domains, and none of the 11 `SortKey` in the repo binds the cursor to a
filter; this is the first. And `GET /bug-reports` without a filter and
`GET /bug-reports/me` share one `SortKey` (`_LIST_SORT`), so a cursor
from one is valid on the other; that must keep working, and no cursor
already issued may stop decoding.

## Decision

**`SortKey` gains an optional field, `scope: str | None = None`, and
`_sort_tag()` appends it to the tag only when it is set.** Without a
filter the tag is `created_at:desc`, exactly as before; with a filter it
is `created_at:desc:status=open`. The comparison of `payload["s"]`
against the tag that `_cursor_from_payload()` already does produces the
422 `invalid_cursor` for every crossing: between two filter values, from
a filtered cursor to no filter, from no filter to a filtered one, and
from a filtered cursor to `/me`, which keeps `_LIST_SORT` with no scope.
The helper never applies the scope: the service still adds the
`.eq("status", ...)` itself, and `keyset_filter`, `through_cursor_filter`,
`apply_page`, `build_page` and `decode_cursor` do not read it. The
filtered `SortKey` objects are declared once, at module level, from
`get_args(BugStatus)`, like the other constants.

**Alternatives rejected:**

- *A new claim in the payload, as `"c"` is for the likes checkpoint
  (ADR 011).* It changes the set of keys the cursor requires, so either
  every cursor of the domain gains a key and the ones already issued stop
  decoding, or the decoder accepts two shapes. It needs new decode code
  and a new rule in `_cursor_from_payload()` for something the tag
  already expresses: "this cursor belongs to this query".
- *Validating the filter in the domain, by decoding the cursor and
  comparing.* The helper does not expose the decoded payload as public
  data, so the domain would have to read it, or the helper would grow an
  accessor only for this. Every future filtered list would repeat the
  comparison, and forgetting it would be a silent wrong page rather than
  a 422. It also moves the cursor check out of the one place that owns
  it.

## Consequences

- No cursor already issued is invalidated: a `SortKey` without scope keeps
  its tag, its keys `{k, i, s}` and its bytes. A helper test pins the
  exact encoding of a cursor without checkpoint, and it passes unmodified.
  This differs from the consequence of the tag (#153) and of the claim
  `"c"` (ADR 011).
- An unfiltered `GET /bug-reports` cursor stays valid on
  `GET /bug-reports/me` and vice versa.
- A filtered cursor is 422 `invalid_cursor` on `/me`, on the unfiltered
  list and under another status.
- A new value in `BugStatus` gets its `SortKey` without touching the map,
  since it is built from `get_args(BugStatus)`.
- Another domain that filters a list can reuse `scope` by declaring a
  `SortKey` per filter value; the helper does not change.

## A repeated `status` is a 422, here only

`?status=open&status=closed` is rejected as 422 `invalid_request` by a
small dependency in `routes/bug_reports.py` that reads
`request.query_params.getlist("status")`. This is local to
`GET /bug-reports` and is not a general rule. Measured on the repo's
FastAPI (0.139.2, pydantic 2.13.4), a `Literal` query parameter given
twice is not rejected: the last value wins (`?status=open&status=closed`
answers 200 with `closed`). Other repeated query parameters keep that
behavior, and this ADR does not change it. Here the 422 is needed
because a filter that silently takes the last of two values, while the
cursor is tied to one of them, would be easy for a client to get wrong
without noticing. For that reason `docs/api/conventions.md` does not
change for it; the behavior is documented in `docs/api/bug-reports.md`.
An empty `?status=` already is 422 `invalid_request` through the type.
