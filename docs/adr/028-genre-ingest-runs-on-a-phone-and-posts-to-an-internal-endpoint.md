# 028. The genre ingest runs on a phone and posts to an internal endpoint with a static token

Where the code that fills the genre playlists runs, how the back lets it in,
and in what order the back checks the request (issue genre-ingest). It
supersedes the assumption in 015 that the genre loader is a script outside
this repo (see the Corrections in this directory's README).

## Context

The genre playlists were loaded by a script outside the repo that wrote to the
database with a service key. Rebuilding them means reading source playlists
from the external provider and finding, for each video, the audio of the
official artist. The responses seen from a cloud environment cannot be
assumed to match the ones the app's users get (`docs/deploy.md`), so the
reading is not done from a cloud job. The write has to follow the track artists rule (015), be
repeatable, and leave the app serving the last good playlists if anything
fails.

## Decision

**The collector runs on the owner's phone, not in a cloud job.** The reading
is done from a regular consumer connection, the same kind the app's users
have. The phone runs `ingest/device/collector.py` from cron, which
reads the source playlists, resolves the audio versions, writes a CSV report
and, with `--write`, sends one submission. The file is standalone (standard
library, `requests` and the provider library) and imports nothing from the
back, so it can be copied to the phone alone. It checks the back's limits and
patterns itself before sending, because the back answers a rejected body with
a 422 and no detail.

**The back only validates and writes.** `POST /ingest/genres` validates the
submission with strict models, checks that the genres exist and writes with
one call to `replace_genre_playlists` (migration `052`), which is atomic and
gives the same rows for the same body, so a retry after a cut connection is
safe. The phone holds no database key.

**The endpoint is internal and authenticated with a static token.**
`INGEST_TOKEN` is compared in constant time (`hmac.compare_digest`). Without
it configured nothing is accepted. The token opens one door, `/ingest/*`; a
user JWT does not open it, and the token opens nothing else. The write uses
its own database client with a longer read timeout, because the RPC is long.

**The token is checked before the body.** The router-level dependency runs
first, and the body is read in a dependency of the route, not declared as a
parameter: FastAPI parses a declared body before it resolves dependencies, so
a malformed JSON would answer 422 to a caller with no token. This way a caller
without the token always gets 401 and the body is never read. Because FastAPI
does not see a body read in a dependency, the route declares it for OpenAPI
with `openapi_extra`.

## Alternatives rejected

- **A cloud job (a scheduled container or function).** Its responses could
  differ from the ones the app's users get, so the playlists could not be
  trusted.
- **The phone writing to the database with a key.** A service key on a phone
  is far more power than the job needs. A leaked ingest token can only submit
  a body that passes the same validation.
- **A user JWT or a database role for the collector.** The collector is not a
  user, and a role would need its own grants on tables the back already
  writes through one function. A static token is enough for one caller and one
  door, and it is rotated by changing one environment variable.
- **Declaring the body as a route parameter.** It is simpler and shows up in
  OpenAPI by itself, but it answers 422 before 401 to anyone, which tells an
  unauthenticated caller about the shape of the body.

## Consequences

- If the phone is off or the cron does not run, nothing is deleted: the app
  keeps serving the last saved playlists.
- The token is a shared secret in the phone's config file (mode 0600) and in
  the back's environment. Rotating it means editing both.
- An ingest rejected by the back says only `invalid_request`. The collector's
  own checks are what name the genre, the playlist and the limit, and a test
  compares its constants with `models/ingest.py` so they cannot drift apart.
- The body of the endpoint is documented through `openapi_extra`, a copy of
  the model's schema that must stay in sync with the route; a test pins it.
- `replace_genre_playlists` takes a fixed advisory lock, so two ingests at the
  same time are serialized and not failed.
