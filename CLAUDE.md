# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Beatly API — backend for Beatly, a music streaming app. A FastAPI service that aggregates music metadata and audio streams and serves them to the mobile and desktop clients.

**Status**: early rewrite of a previous, now-unmaintained backend. Endpoints are being ported one at a time, each rewritten to follow the response contract below. `routes/`, `services/`, `test/`, and `docs/` are currently empty placeholder directories for this ongoing port; `middlewares/` is also empty. The only implemented pieces so far are the app entrypoint and the response/error contract in `core/` and `models/`.

## Commands

Activate the venv first (already created at `venv/`):

```
source venv/bin/activate
```

Run the dev server (interactive docs at http://localhost:8000/docs):

```
uvicorn app:app --reload
```

Test and lint:

```
pytest
pytest test/test_foo.py::test_name   # single test
ruff check .
ruff format .
```

There is no `pyproject.toml`/`ruff.toml` yet, so ruff runs with default rules.

## Architecture

**Response contract**: every JSON response follows one of two shapes, defined as Pydantic models in `models/responses.py`:
- `ApiSuccess[T]` — `{ok: true, data: T}`, built via `ok_response(data)`.
- `ApiError` — `{ok: false, reason: str}`, built via `error_response(reason)`.

Clients check `ok` in the body, not the status code, to distinguish "nothing here" from "navigation error". Status code conventions:

| Case                                                   | Status |
| ------------------------------------------------------- | ------ |
| Success                                                | 200    |
| Expected empty state (parent exists, no sub-resource)  | 200 (`ok: false`) |
| Parent resource does not exist                         | 404    |
| Invalid input                                          | 422    |
| Upstream service failed                                | 502    |
| Upstream timed out                                     | 504    |
| Unhandled internal error                               | 500    |

Example: a track that exists but has no lyrics returns 200 with `ok: false`; a track id that does not exist returns 404.

**Layering rule**: routers (`routes/`) own all HTTP logic. Services (`services/`) never import `HTTPException`, never build responses, never know about status codes — they return domain data or raise domain exceptions from `core/exceptions.py`. Keep this boundary when porting endpoints from the old backend.

**Error handling**: domain errors are modeled as exceptions in `core/exceptions.py`. `AppError` is the base class; each subclass (`NotFound`, `InvalidRequest`, `Unauthorized`, `Forbidden`, `Conflict`, `UpstreamError`, `UpstreamTimeout`, `ResourceEmpty`, ...) fixes a `status_code` and a default `reason` string. Raise these from routes/services instead of `HTTPException` or building error responses manually — raising `NotFound()` (or `NotFound("custom_reason")`) is enough; the mapping to an HTTP status and JSON body happens centrally.

`app.py` wires four global exception handlers that are the only place HTTP responses get constructed from exceptions:
- `AppError` → status from `exc.status_code`, body from `error_response(exc.reason)`. 5xx errors are logged with `logger.exception`, others with `logger.warning`.
- `StarletteHTTPException` → reason looked up from `HTTP_REASONS` (status code → reason string), same envelope.
- `RequestValidationError` (422) → always `error_response("invalid_request")`, logged as a warning.
- Bare `Exception` (catch-all) → 500 with `error_response("internal_error")`, logged with `logger.exception`.

Error responses must never expose exception messages, stack traces, or other internal details to the client — those details go to the server log only, via the handlers above.

When adding a new failure mode, prefer adding a new `AppError` subclass (and, if it maps from a raw HTTP status Starlette can raise directly, an entry in `HTTP_REASONS`) over handling it ad hoc in a route.

**Logging**: `setup_logging()` in `core/logging.py` is called once at import time in `app.py`, before anything else runs. It clears any existing root handlers and installs a single `StreamHandler` to stdout with a fixed format. Get loggers with `logging.getLogger(__name__)`; never use `print()`; don't call `setup_logging()` again or configure logging elsewhere.

**Adding routes**: `app.py` currently defines `/health` directly on `app`. As routes are ported into `routes/` (one file per domain), wire them up as `APIRouter`s included from `app.py` rather than growing `app.py` itself. `test/` should mirror the `routes/`/`services/` layout, and every endpoint should ship with tests for its three cases: with data, expected empty, and parent-not-found.
