# 002. Nonexistent album id maps to upstream_error

Why `GET /album/{album_id}` responds 502, not 404, when a well-formed
id does not match any album.

## Context

`get_album` on the external provider validates the id's prefix and
then navigates the page it fetches. A well-formed id that does not
exist returns a page without the sections the parser expects, which
raises a `KeyError` from the provider's own navigation path. That
`KeyError` is **indistinguishable** from the one a layout change on the
provider's side would raise: same exception class, same place, nothing
in the payload that tells the two apart.

## Decision

That `KeyError` is translated to `upstream_error` (502) by
`translate_upstream_errors()`, the same as any other provider failure.
It is not mapped to 404. `GET /album/{album_id}` has no domain 404.

## Consequences

Why the alternatives were rejected:

- Mapping to 404 optimizes for the rare case (an id typed by hand or
  copied stale) at the cost of hiding the expensive one (the provider's
  layout broke): a 404 is an expected, unalarming response, so a spike
  of them from a broken parser would not page anyone, while a spike of
  502s would.
- Trying to tell the two cases apart by inspecting the exception or the
  partial payload would tie this repo to the internal shape of the
  provider's response — exactly what changes when the provider's
  layout changes, which is the failure this decision is trying to keep
  visible.
- A 404 here would also conflict with this repo's own convention that
  404 means "the parent resource does not exist" according to our own
  data, not an inference drawn from a third party's response shape.

What is accepted: a client asking for an id that plainly does not
exist is told "the service is failing" instead of "it does not exist",
and cannot tell the two situations apart from the status code alone.
This is revisited if the external provider ever starts distinguishing
the two cases explicitly in its response.
