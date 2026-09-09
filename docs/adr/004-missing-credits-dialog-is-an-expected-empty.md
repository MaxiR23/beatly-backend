# 004. Missing credits dialog is an expected empty

Why a navigation failure inside `get_song_credits` on
`GET /tracks/{track_id}/credits` becomes a 200 with empty credits when
the ADR 003 probe confirms the track exists, and a 404 when it does
not.

## Context

`/credits` does not go through `get_watch_playlist`, so the ADR 003
call site for the probe does not serve it as-is: this endpoint builds
its own browse id (`"MPTC"` + the audio track id) and calls
`get_song_credits` directly. That library call can fail in two ways,
and neither carries a field that tells them apart:

- An HTTP >= 400 from the provider's backend, raised as
  `YTMusicServerError` — the same class ADR 003 already intercepts in
  `provider_get_watch_playlist`.
- A navigation failure over a 200 response, raised as `KeyError` or
  `IndexError` while walking the fixed path to the credits dialog.

That second path has three possible causes: the song exists and
genuinely has no credits on the provider (the issue that motivated
this endpoint confirms the provider signals that as an exception, not
as an empty payload), the `track_id` does not exist, or the provider
changed the page's layout. Reading the exception's message to tell
these apart is the antipattern this repo already rejected.

## Decision

The ADR 003 probe is wired into **both** failure paths of
`get_song_credits`, with the same rule: `status == "ERROR"` -> 404
`track_not_found`. On the navigation-failure path, and **only** there,
if the probe reports any other status the endpoint returns 200 with
empty credits. If the probe itself fails, the original failure or the
probe's own failure surfaces as 502/504. An empty response is never
returned without the probe positively confirming the track exists.

## Consequences

- **No measurement of which of the two paths a nonexistent `track_id`
  takes was needed.** Wiring the probe into both means the 404 comes
  out correct either way. That measurement is left as a manual QA
  item, not a premise of the design.
- **What is accepted, and is the real cost:** a provider-side layout
  change on a track that exists is reported as "no credits" (200
  empty) instead of 502. The empty branch covers **any** navigation
  failure inside `get_song_credits`, including the four accesses in
  its section loop, not only the absence of the dialog itself. This is
  accepted because the alternative — always 502 on this path — breaks
  the explicit 200-empty acceptance criterion, and because the case is
  bounded: no `YTMusicServerError`, no timeout, and the track is
  confirmed to exist by a second endpoint. The bound matters because
  the symptom is visible and was already lived once: the previous
  backend reported "no credits" on **every** track, and investigating
  that symptom is exactly what led to discovering the `'sections'`
  substring-matching bug. Reporting 502 on every navigation failure
  instead would trade that mistake for a worse one: a track without
  credits, which is legitimate and common, would be reported as the
  service being down.
  That precedent was a total failure, visible on its own because it hit
  every track. A layout change confined to the section loop is not: it
  can affect only some tracks, and on its own it is indistinguishable
  from the common, legitimate "this track has no credits" case. What
  makes it observable here is not a single event but the **rate** of
  this branch: a handful of hits a day is the ordinary background of
  songs without credits, all of them at once or a rate that jumps is
  the layout changing. `provider_get_song_credits`
  (`core/search_provider.py`) logs one `INFO` line per hit on this
  branch, with `video_id` and `type(exc).__name__` and never the
  exception's message, so that rate is a real, queryable signal instead
  of relying on the symptom carrying over from the total-failure
  precedent above.
- **Contrast with ADR 003, which this record does not supersede or
  correct:** there the probe only runs on the error path, and the
  happy path never pays for an extra call. Here it also runs on the
  **empty** path, so a track with no credits costs two calls to the
  provider instead of one. That is the price of telling the empty
  apart from the 404 without reading a message.
- The browse id mechanism (`"MPTC"` + the audio track id) is tied to a
  third party's format. If the provider changes it, the failure mode
  is a 200 empty or a 502 on tracks that do have credits, never a 404
  on a valid track.
