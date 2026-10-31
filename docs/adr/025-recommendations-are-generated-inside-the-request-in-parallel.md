# 025. Recommendations are generated inside the request, in parallel

Why `POST /recommendations/generate` does its provider calls inside the
request instead of in the background, how the parallel stages share the
request budget, and how a seed that no longer exists is skipped without
hiding a failure of the provider (issue #221).

## Context

A personalized set is made from the pages of up to 5 seed artists, 15
similar artists and up to 3 history albums: about 23 calls to the external
provider. The request budget is 12 s (ADR 024) under the app's 15 s.

Measured on 2026-10-30 from the owner's machine, the provider library
directly (no app, no Redis, no database), a new client per run (no cache),
23 calls per run, two runs per row:

| threads | seeds | similars | 3 albums | total | p50 per call | max per call |
|--------:|------:|---------:|---------:|------:|-------------:|-------------:|
| 1       | 1.51  | 3.37     | 0.53     | 5.40  | 0.218        | 0.586        |
| 1       | 1.51  | 3.47     | 0.56     | 5.53  | 0.226        | 0.620        |
| 4       | 0.83  | 1.04     | 0.24     | 2.11  | 0.249        | 0.662        |
| 4       | 0.84  | 0.97     | 0.23     | 2.04  | 0.236        | 0.776        |
| 8       | 0.66  | 0.63     | 0.22     | 1.51  | 0.240        | 0.663        |
| 8       | 0.63  | 0.57     | 0.22     | 1.42  | 0.233        | 0.624        |

(seconds). The latency of a call does not grow with the concurrency (p50
0.22-0.25 s in every row).

## Decision

**Inside the request.** The generation runs in the `POST`. With 4 workers it
takes about 2.1 s, 5.7 times under the 12 s budget. The worst case, with each
of the 7 batches (2 + 4 + 1) paying the slowest call measured (0.776 s), is
5.4 s, still 2.2 times under. The pages go through the same server-side
cache as `/artist/{id}` (12 h).

That worst case assumes no id is missing. Every nonexistent id adds a probe
call, and every nonexistent similar artist adds one more round of stage 2, all
inside the same 12 s budget. In the worst case the generation ends in a 504
and stores nothing, like any other timeout.

**Three parallel stages.** The seeds' pages, then the pages of the chosen
similar artists, then the history albums. Four workers by default
(`RECOMMENDATIONS_PARALLEL_CALLS`, 1 to 8, 8 being the most measured). All the
inputs are read from the database first and everything is computed before one
single call to `replace_user_recommendations`, which deletes the user's rows
and inserts the new set in one transaction. Any failure before that call is a
502 or 504 that writes nothing, so the previous set is intact by
construction.

**The budget follows the workers.** Each task is submitted as
`contextvars.copy_context().run(...)`, one copy per task (a `Context` cannot be
entered by two threads at once). The `ContextVar` of ADR 024 stores a mutable
holder, so every copy sees and respects the same deadline: a worker that
starts after the budget is spent raises `ProviderDeadlineExceeded`, which
ends as a 504. An executor of our own without copying the context would run
its calls with no budget; ADR 024 warns about that and
`test_generate_worker_calls_share_the_request_budget` pins it. After the first
failure the pending calls are cancelled and do not start; the ones already
running end bounded by the budget.

**The workers do not note images.** `cache_set()` serializes the `Artist` or
`Album` it caches, which runs the computed `image` field and notes every URL in
the request's color scope. A worker sees the same scope object, so one cold
`POST` would note the images of 20 artist pages (songs, singles, albums,
related: hundreds of URLs) and the per-request cap of image colors would be
spent on images that are not in the response. Each worker calls
`detach_image_color_scope()` inside its copy of the context; the request and
the serialization of its response keep their scope.

**An executor per generation.** The only other `ThreadPoolExecutor` of the repo
(`services/image_color_service.py`) is one per process, because it limits the
downloads of a background task that has no hurry. Here a pool per process of
4 threads would queue the generations of different users, and the queue would
eat the 12 s of each request. The cost is up to 4 extra threads per
simultaneous `POST`, besides the 40 of the threadpool. (Note 2026-10-30:
`services/onboarding_service.py` now has a third executor, one per request with
up to 4 threads.)

**A seed that is gone is skipped, with proof.** An id the provider does not
have used to be indistinguishable from a failure of the provider: the library
raises `KeyError` on the navigation of the page, the same as for a changed
layout (ADR 002 keeps `/artist/{id}` and `/album/{id}` at 502 for that
reason, and they stay so). Measured on 2026-10-30, raw `browse` requests:

| case | status | top-level keys | `microformat.microformatDataRenderer` | the library |
|---|---|---|---|---|
| real artist, real album | 200 | `contents`, ... | `urlCanonical`, `title`, ... | ok |
| 2 made-up artist ids (`UC...`) | 200 | `microformat`, `responseContext`, `trackingParams` | exactly `{"noindex": true}` | `KeyError('contents')` |
| 2 made-up album ids (`MPREb_...`) | 200 | the same | exactly `{"noindex": true}` | `KeyError('contents')` |
| made-up artist id with the `MPLA` prefix | the library strips the prefix; the raw request with it is 401 | - | - | `KeyError('contents')` |

So `core/search_provider.py` has `provider_raise_if_browse_missing`, called
only after the real operation failed (the happy path never pays for it): it
requests the raw page and raises `ProviderResourceMissing` only when there is
no `contents` and the renderer is exactly `{"noindex": true}`, both at once.
A layout change that breaks the library's parsing would keep the
`urlCanonical`, so it is not mistaken for a missing id; a response without
`microformat` is a shape not measured and ends in a 502. The service only
probes when the cause of the failure is a `KeyError` (the one form a missing
id takes, measured in 5 of 5 cases): a server error, a parse error or a
network failure is not probed, and a timeout (`UpstreamTimeout`, a sibling
class of `UpstreamError`) never is. A confirmed id returns `None`, one `INFO`
line says the kind (`seed_artist`, `similar_artist`, `history_album`) and the
id, and the rounds spread over the rest. This `None` is an empty with proof,
like the credits of `provider_get_song_credits`, not an `except` that
swallows an error. `provider_get_artist` and `provider_get_album` do not
change: `ProviderResourceMissing` is not in `core/upstream.py`'s classes, so
raising it there would be a 500 on `/artist` and `/album`.

The probe calls `_send_request`, a private method of the library: the first
such call in the repo. The library has no public `browse` that returns the raw
page, and the signal is exactly what its parser drops. The version is pinned in
`requirements.txt` and `test_the_library_still_exposes_send_request_with_endpoint_and_body`
fails if a new version renames its first two parameters. If the probe fails the
generation ends in 502, so it never skips a seed without proof.

## Alternatives rejected

- **Background generation with a state** (generating / ready). It needs a
  place for the state, a rule for a second `POST` while one is running (409),
  and polling from the client. None of it is needed with a 5.7x margin.
- **In series, inside the request.** 5.4 s with no queue and no extra
  threads, but 2.6 times slower than 4 workers (5.4 s against 2.1 s), with
  less margin under the 12 s for the slow tail.
- **Skip a seed on any failure.** It would also skip a change of layout of the
  provider, which ADR 002 wants visible: every seed would "go missing" and the
  generation would end empty instead of failing.

## Consequences

- Up to `RECOMMENDATIONS_PARALLEL_CALLS` extra threads per simultaneous
  `POST`. (Note 2026-10-30: `POST /onboarding/complete` adds up to 4 more per
  request, from its own copy of the helpers.) (Note 2026-10-30: `POST
  /onboarding/similar` adds 1 more per request, the same executor with one
  item.)
- The burst was measured up to 8 threads and from one machine, not with
  several users at once from the server. `provider_slow` (ADR 024) warns if
  the latency rises.
- The same provider client is now shared by several threads inside one
  request too. Its `base_headers` is a cached property that asks for a visitor
  id on the first use of the process: with several cold threads up to 4
  duplicated requests can go out, which is harmless (the measurement used a
  new client per run and included it).
- With Redis hung, every `get_artist` pays up to 2 x 0.25 s (ADR 006): about
  3.5 s over 7 batches, outside the provider budget but inside the app's 15 s.
- A user with no seeds, or whose seeds are all gone, gets an empty set and an
  older expired set stays visible in `GET`.
- The tier of a set (how many days it is valid) counts the plays since the
  previous generation, in a function of the database, because the `count` of
  PostgREST would be the only other option and the other four reads of the
  generation are functions too.
