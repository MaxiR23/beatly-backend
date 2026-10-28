# 022. Image colors are computed in parallel, written one by one, and shorten the cache while pending

Why every image tier URL must load, why the main image of a response is
computed first, why the computation is an `async` task over a pool of its own
with one write per image, and why a response with a color still to be computed
carries a short `max-age` (issue #213, built on #211 and #212).

## Context

Measurements taken on #211 against the provider's CDN, with real covers, found
two problems. The size rule of ADR 019 turned `=w60-h60-s-l90-rj` into
`=w544-h544-p-s-l90-rj`, and the CDN answers 400 to `-p` followed by `-s` or
`-dc...`: the tiers of those covers never loaded and their color was never
computed. And the color of a cover that nobody had seen before took 570 to
3444 ms to be saved after the response, in the cases where it was saved at all
(6 of 12 on the first visit), because the cover came 11th in a serial queue of
related items. A client holding the response under `max-age` kept `color:
null` for hours.

## Decision

**The URL rule keeps `-p` and an allow list of options.** The first size suffix
and the options after it are replaced by `=w<size>-h<size>-p` plus the options
in `l<digits>`, `rj` and `rw`; the rest is dropped. Four rules were measured on
50 real covers (the 24 of 50 that failed with the old rule included, and the
size 192, 600 and 1320 tiers, the 544 and the 512 of recents): the old rule
broke 24 of 50, and the allow list, a deny list (`-s`, `-dc...`) and "keep the
options and drop `-p`" broke 0 of 50, all square. The allow list wins by
preference: an option nobody measured is dropped, which gives a URL that
loads, instead of kept, which could give one that does not. `rw` was added to
the first idea (`l<n>`, `rj`) because the remote database stores recents
with `-l94-rw` and `-p-l94-rw` (45 rows, none with `-s` or `-dc`), measured
200 in the three sizes. `image_key` and `smart_crop=False` do not change, so
the colors already stored keep their keys and migration `036` still matches.

**The main image goes first and is never cut.** `load_image_colors` orders
`scope.pending` with the `image` at the top of `data` and the elements of its
`mosaic` first (a search result has none), and the per-response limit is
`max(image_color_max_per_request, number of main images)`. The database does
not promise the order of what `claim_image_colors` returns, so the service
queues the claimed keys in the order of the candidates.

**An `async` task over a pool of its own.** `compute_image_colors` is a
coroutine: it claims in a thread (`asyncio.to_thread`), queues one job per
claimed key in a `ThreadPoolExecutor` of `image_color_max_parallel_fetches`
threads (default 4) and awaits them. Each job downloads, computes and upserts
the row of its own image, so a color is saved as soon as it is ready and the
failure of one image (download, decode or write) does not touch the others.
A color is an upsert; a failure is an `update` of the row (the claim inserted
it) conditioned on `color IS NULL` and on `attempted_at` still being the
job's own claim, so a failure never overwrites a color a second job stored
meanwhile, nor marks as failed a key another job claimed again (see
"Consequences").
That pool is the only limit on simultaneous downloads of a process: the
`BoundedSemaphore` and `image_color_max_concurrency` are gone, and with them
the task that found no slot and gave up, which lost the cover under load. A
task waits as a coroutine, so it no longer occupies a thread of the
threadpool the requests share. The pool's queue has no bound, and that is what
replaces the give-up for lack of a slot: a job waits instead of being dropped.
The queue is FIFO between batches.

**A short `max-age` while a color is pending.** `ImageColorScope` knows
`awaiting_colors`: a pending key (eligible, not stored, not failed inside the
24 h window), a key another request claimed less than `IMAGE_COLOR_ABANDONED_AFTER` ago (30
s) and has not written yet (`color` null and `failed_at` null, read from
`get_image_colors` since `043`) or a failed read. Those keys are not claimed
or queued again. Past that time the claim counts as abandoned: the key is
pending again and the next request claims it atomically
(`claim_image_colors` with `p_abandoned_before`, since `044`) and computes it.
The main image goes first and is never cut by the limit. A failed key keeps
the 24 h window. Why 30 s: ten times the download timeout (3 s) and about
thirty times what a full cold batch takes (about 1 s, below). The setting
fails at startup outside `(image_color_fetch_timeout, image_color_retry_after]`. A
main image never goes out with `color: null` and the long `max-age`, except
for a permanent null. `with_image_colors` then calls `cap_max_age` on
the response of the route, which rewrites a plain `max-age=<N>` to
`min(N, IMAGE_COLOR_PENDING_MAX_AGE)` (default 5 s: the goal is for the color to be stored in under 1 s at p95, and a
full batch of 10 with 4 downloads in parallel takes about 1 s cold, around
300 ms per download, so 5 s leaves room for the client to ask again and find
it; 0 is valid and means always revalidate). The route does not change:
the dependency receives the same `Response` the route does. `private,
no-cache`, `no-store` and the errors are untouched. `/tracks/{id}/upnext`
(`private, max-age=<N>`) is left out on purpose: the front pins its suggestions
per source (front #84) and a short value would bring back the problem that
fixed. A permanent null does not shorten anything, or the client would ask
again forever.

**Recents.** The 45 stored recents (`-l94-rw`: 37, `-p-l90-rj`: 7, `-p-l94-rw`:
1) are valid with the new rule, so no data migration was written.

## Replaces

This record replaces, from `021`: the part of "Computed in a background task"
about the semaphore of 2 computations, the task that gives up without a slot
and the single write at the end; and the Consequences bullets "A response a
client holds under `Cache-Control: max-age` can keep `null` until it expires"
and "The background task uses the request threadpool; the semaphore (2)...".
It also replaces the sentence of the Decision "A claim in the database, with a
retry window" "A key claimed by a request that died in the middle waits for
the whole window." and the Consequences bullet "A key whose claimer died in the
middle waits the whole 24 h window; two processes never compute the same key
inside it.".

## Consequences

- Up to 4 simultaneous downloads per process, up to 4 threads of its own.
- 1 + N calls to the database per batch (a claim and an upsert per image)
  instead of 2. They run in the background and in parallel.
- A request that arrives while another is computing the same key goes out
  with the short `max-age`, for the whole wait in the pool's queue plus the
  job itself.
- A row left without color and without `failed_at` (a forced shutdown, a
  failed write in `_compute_one`, or an exception outside the tuple it
  catches, which escapes before the write) counts as in progress during
  `IMAGE_COLOR_ABANDONED_AFTER`, with the short `max-age`. After that the next
  request claims it and computes it. No cover is left without color by a cut
  job.
- A job still in the queue for over 30 s (backlog) is claimed again and the
  image can be downloaded twice, at most once per key every 30 s because the
  new claim renews `attempted_at`. If both end the same way the result is the
  same. If the first computes the color, the failure of the second does not
  touch it (the `update` is conditioned on `color IS NULL`). If the first
  fails while the second is in progress, its `update` does not match either
  (it is conditioned on its own `attempted_at`) and the second decides the
  result. A late color of the first stays stored. The cost is only the extra
  download, under the load that caused the backlog. Accepted.
- Migrations `043` and then `044` are applied BEFORE the code. `043`: the new
  code without it fails the validation of every read, so every response goes
  out with `color: null` and a short max-age, one warning per request, and
  none fails. `044`: the new code without it gets `PGRST202` on every claim,
  so no new color is computed and every response with an image without color
  goes out with the short max-age; none fails.
- A graceful shutdown waits for the background tasks, queued jobs included
  (uvicorn has no graceful timeout by default); only a forced stop, or a
  configured `timeout_graceful_shutdown` that runs out, abandons them, and
  their keys are claimed again by the first request after
  `IMAGE_COLOR_ABANDONED_AFTER`.
- A `.env` that still has `IMAGE_COLOR_MAX_CONCURRENCY` starts, and the
  variable no longer does anything.
- Clients that keep URLs (for example as a key of their image cache) see new
  ones once for the covers that carried `-s` or `-dc...`.
- Measured locally, same method before and after: the first visit to 12 new
  albums saves 12 of 12 covers (6 of 12 in #211 and 8 of 12 in the baseline of
  this issue), p95 180 ms from the response (417 ms in the baseline).
