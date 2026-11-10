# Deploy

How the API is packaged and run as a container. Nothing in the repo depends
on a cloud provider; the Cloud Run and App Runner sections are a guide only.

## Image

The `Dockerfile` builds a two-stage image on `python:3.14-slim`. The build
stage installs `requirements.txt` into a venv; the final stage copies that
venv and the application code, and runs as the non-root user `10001`.

- Contains: the runtime venv, `app.py`, `core/`, `models/`, `routes/` and
  `services/`.
- Does not contain: `.env`, tests, docs, `db/`, scripts, a compiler or any
  development tool. The `COPY` lines are an explicit list and
  `.dockerignore` is a second barrier, so a local `.env` cannot reach the
  image. No key is baked in; configuration is read at run time.
- `requirements.txt` is runtime only. `requirements-dev.txt` is
  `-r requirements.txt` plus pytest, ruff, pre-commit and the other
  development tools.

## Build and run

    docker build -t beatly-api .
    docker run --rm -p 8000:8000 --env-file <file> beatly-api

To deploy, build for the platform the cloud runs, amd64:

    docker build --platform linux/amd64 -t beatly-api .

or use the image the CI `image` job builds, which is amd64. On an Apple
silicon Mac, a build without `--platform` produces an arm64 image: it is
fine to test locally but it is not the one to deploy.

The compose file also has an optional `api` service under the `image`
profile, next to Redis:

    docker compose --profile image up --build

`docker compose up -d redis` is unchanged and does not start it. Port 8000 is
shared with a local `uvicorn app:app --reload`: both cannot run at once.

## Environment

Required; without them the process does not start:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_JWT_SECRET`
- `SUPABASE_ANON_KEY`

Optional, with defaults and ranges in `.env.example`: `ENV`, `LOG_LEVEL`,
`REDIS_URL`, `SUPABASE_CONNECT_TIMEOUT`, `SUPABASE_READ_TIMEOUT`,
`SUPABASE_WRITE_TIMEOUT`, `SUPABASE_POOL_TIMEOUT`, `SUPABASE_REQUEST_BUDGET`,
`SUPABASE_SLOW_REQUEST_THRESHOLD`, `PROVIDER_CONNECT_TIMEOUT`,
`PROVIDER_READ_TIMEOUT`, `PROVIDER_REQUEST_BUDGET`,
`PROVIDER_SLOW_REQUEST_THRESHOLD`, `GZIP_MINIMUM_SIZE`, `GZIP_COMPRESSLEVEL`,
`IMAGE_COLOR_ALLOWED_HOSTS`, `IMAGE_COLOR_FETCH_TIMEOUT`,
`IMAGE_COLOR_MAX_BYTES`, `IMAGE_COLOR_MAX_PIXELS`, `IMAGE_COLOR_RETRY_AFTER`,
`IMAGE_COLOR_ABANDONED_AFTER`, `IMAGE_COLOR_MAX_PER_REQUEST`,
`IMAGE_COLOR_MAX_PARALLEL_FETCHES`, `IMAGE_COLOR_PENDING_MAX_AGE`,
`IMAGE_COLOR_BACKGROUND`, `IMAGE_COLOR_MIN_BACKGROUND_CONTRAST`,
`IMAGE_COLOR_MIN_WHITE_TEXT_CONTRAST`, `RECOMMENDATIONS_TIER_MID_PLAYS`,
`RECOMMENDATIONS_TIER_HIGH_PLAYS`, `RECOMMENDATIONS_TIER_HIGH_DAYS`,
`RECOMMENDATIONS_TIER_MID_DAYS`, `RECOMMENDATIONS_TIER_LOW_DAYS`,
`RECOMMENDATIONS_SEED_WINDOW_DAYS`, `RECOMMENDATIONS_EXCLUDE_WINDOW_DAYS`,
`RECOMMENDATIONS_EXCLUDE_TOP_ARTISTS`, `RECOMMENDATIONS_EXCLUDE_MIN_PLAYS`,
`RECOMMENDATIONS_HISTORY_ALBUMS`, `RECOMMENDATIONS_PARALLEL_CALLS`,
`INGEST_TOKEN`, `INGEST_RPC_TIMEOUT`.

Three of them need attention in a container:

- `REDIS_URL`: inside a container the default `localhost` is not a Redis.
  Without a reachable Redis the API still works, just without cache. Set it
  to the Redis the container can reach.
- `IMAGE_COLOR_ALLOWED_HOSTS`: without it there are no image colors. Set the
  hosts of the images the API may fetch.
- `INGEST_TOKEN`: without it `POST /ingest/genres` answers 401 to everything,
  and the collector cannot fill the genre playlists. The platform's request
  timeout must be above `INGEST_RPC_TIMEOUT`, or it cuts the write before the
  database ends. See [the ingest guide](ingest.md).

Keys are injected through the platform's secret manager, never into the
image.

## Server process

These are read by the launcher, not by the app, so they are not in
`.env.example` (a `uvicorn app:app --reload` ignores `PORT`).

- `PORT`: port to listen on, default 8000. The server binds `0.0.0.0`.
- `WEB_CONCURRENCY`: number of uvicorn workers, default 1. Uvicorn reads it
  natively.
- `FORWARDED_ALLOW_IPS`: which peers may set `X-Forwarded-*` headers, default
  `127.0.0.1`. Uvicorn reads it natively. With the default, the headers sent
  by the platform proxy are ignored, so the app sees every request as plain
  `http` from the proxy's address. That shows in two places
  today: the access log, and the automatic trailing-slash redirect, which
  answers `GET /likes/` with a 307 to `http://<host>/likes`, an `https`
  downgrade. Calling the paths without a trailing slash avoids it.
  Set it to the proxy's IP or network, never `*` without knowing who can
  reach the container. On platforms where only the platform's own proxy
  can reach the container, such as Cloud Run and App Runner, set
  `FORWARDED_ALLOW_IPS='*'` so the app sees `https` and the redirects keep
  it. Uvicorn documents `*` as safe only when every forwarded header can be
  trusted
  (<https://uvicorn.dev/deployment/#proxies-and-forwarded-headers>); on a
  VM where clients reach the port directly, use the proxy's address
  instead.

Why one worker: FastAPI's guide for containers
(<https://fastapi.tiangolo.com/deployment/docker/>) recommends one Uvicorn
process per container when the platform already replicates containers, and
scaling by instances; `--workers` is for simple cases such as a single
server. Uvicorn manages its own workers, so Gunicorn is not needed. Anything
per process is multiplied by `WEB_CONCURRENCY`:
`IMAGE_COLOR_MAX_PARALLEL_FETCHES`, the cache of up to 256 user clients in
`core/database.py`, the Redis and Supabase clients and the 40-thread
threadpool. Scale by instances before workers.

## Health and shutdown

`GET /health` is the liveness check. It touches neither Redis, Supabase nor
the external provider, so it says the process is up, not that its
dependencies are. There is no readiness endpoint.

On SIGTERM uvicorn stops accepting connections and waits up to 8 seconds
(`--timeout-graceful-shutdown 8`) for requests in flight; a longer request is
cut. The limit exists because `docker stop` and most platforms send SIGKILL
after 10 seconds. Uvicorn is PID 1 (`exec` in the `CMD`), so it receives the
signal.

## Google Cloud Run (overview)

A guide, not a requirement; confirm it against the platform's documentation
when deploying, and nothing in the repo depends on it.

- Push the amd64 image to a registry and create the service from it.
- Cloud Run injects `PORT`, which the image honors.
- Set the required variables from the platform's secret manager.
- Set `FORWARDED_ALLOW_IPS='*'`: only Cloud Run's proxy reaches the
  container (see Server process).
- Point the health check at `/health`.
- Scale by instances; keep `WEB_CONCURRENCY` at 1.
- Keep the shutdown window in mind: the 8-second limit fits inside it.

## AWS App Runner (overview)

Same caveats as above.

- Create the service from the amd64 image in a registry.
- Configure the port as 8000 (the default).
- Set the required variables from the platform's secret manager.
- Set `FORWARDED_ALLOW_IPS='*'`: only App Runner's proxy reaches the
  container (see Server process).
- Point the health check at `/health`.
- Scale by instances; keep `WEB_CONCURRENCY` at 1.
- Keep the shutdown window in mind: the 8-second limit fits inside it.

## Datacenter IPs

The external provider may answer differently (blocks, challenges, 502) to
datacenter IPs than to a home connection. On the first deploy, call
`/search`, `/album/{id}` and `/tracks/{id}` and compare the responses with
local ones before sending traffic.
