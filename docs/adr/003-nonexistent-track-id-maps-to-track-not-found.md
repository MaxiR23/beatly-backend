# 003. Nonexistent track id maps to track_not_found

Why `GET /tracks/{track_id}/upnext`, `GET /tracks/{track_id}/lyrics`
and `GET /tracks/{track_id}/related` respond 404, not 502, when a
`track_id` does not exist.

## Context

`/upnext`, `/lyrics` and `/related` all go through
`get_watch_playlist`, which raises `YTMusicServerError` with the same
message both for a nonexistent track and for a real HTTP >= 400 from
the provider's backend, and that exception class carries no structured
attribute to tell the two apart. **But** the provider does expose the
distinction elsewhere: `get_song(track_id).playabilityStatus.status`
is a structured field with three measured values — `OK`, `UNPLAYABLE`
and `ERROR` — and `ERROR` is what it returns for an id that does not
exist.

## Decision

A `YTMusicServerError` raised by the watch playlist call triggers
**one** lazy probe to `get_song`. If `status == "ERROR"`, the endpoint
responds 404 `track_not_found`. In any other case, the original error
is re-raised and the endpoint responds 502/504 as usual. The rule is
`== "ERROR"`, **never `!= "OK"`**.

## Consequences

Why this domain can tell the two cases apart and `album` cannot
(explicit contrast with ADR 002, which this record does **not**
supersede): in `album` the only evidence available is a `KeyError`
from navigating the page, indistinguishable from a layout change. Here
there is a field with a value of its own for exactly this case, on a
different endpoint than the one that failed. The condition ADR 002
left written down for revisiting — "if the external provider ever
starts distinguishing the two cases explicitly" — is the one that
holds here, and only here: this record does not change the behavior of
`/album` or `/artist`, because `get_song` is a probe by `videoId` and
neither of those domains has an equivalent id to probe with.

The provider distinguishes `ERROR` from `UNPLAYABLE`: `UNPLAYABLE` is a
track that exists and whose `/upnext`, `/lyrics` and `/related`
endpoints all work, so it responds 200. A reason of its own for
"exists but is not playable" belongs to the audio/streaming domain and
is not added here.

What is accepted:

- An extra network call, but only on the error path.
- A provider failure during the probe turns a 502 into a 502 or a 504
  either way — it can just change which of the two — so a client
  cannot see a 404 out of a genuine provider outage.
- The decision is tied to the values of a third party's field. If the
  provider ever changes what it returns there, the failure mode is a
  502 on a nonexistent id (a client sees "the service failed" instead
  of "not found"), never a 404 on a valid one, because the condition
  is strict equality against one known value, not a negation.
