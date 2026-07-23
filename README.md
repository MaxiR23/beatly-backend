# Beatly API

Backend for Beatly, a music streaming app. FastAPI service that
aggregates music metadata and audio streams and serves them to the
mobile and desktop clients.

## Status

Early rewrite. A previous version of this backend exists but is not
maintained. Endpoints are being ported one at a time, each one
rewritten to follow the response contract below.

## Stack

- Python 3.14, FastAPI
- Pydantic for models and validation
- pytest for tests, ruff for lint and format

## Response contract

Every JSON response follows one of two shapes:

    {"ok": true, "data": ...}
    {"ok": false, "reason": "..."}

Status codes:

| Case                                        | Status |
| ------------------------------------------- | ------ |
| Success                                     | 200    |
| Expected empty state (parent exists, no sub-resource) | 200 |
| Parent resource does not exist              | 404    |
| Invalid input                               | 422    |
| Upstream service failed                     | 502    |
| Upstream timed out                          | 504    |
| Unhandled internal error                    | 500    |

A track that exists but has no lyrics returns 200 with ok:false.
A track id that does not exist returns 404. Clients check `ok` in
the body, not the status code, to decide between "nothing here" and
"navigation error".

## Rules

- Routers own all HTTP logic. Services never import HTTPException,
  never build responses, never know about status codes. They return
  domain data or raise domain exceptions from core/exceptions.py.
- Error responses never expose exception messages, stack traces or
  internal details. Details go to the server log only.
- Use the logger. Never print().
- Every endpoint ships with tests for its three cases: with data,
  expected empty, parent not found.

## Layout

    app.py            entrypoint, global exception handlers
    routes/           HTTP layer, one file per domain
    services/         business logic and external providers
    models/           pydantic models
    core/             config, exceptions, logging
    test/             tests, mirrors the routes/services layout
    docs/             API documentation

## Running

    source venv/bin/activate
    uvicorn app:app --reload

Interactive docs at http://localhost:8000/docs

## Testing

    pytest
    ruff check .
    ruff format .