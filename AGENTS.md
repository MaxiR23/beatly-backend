# AGENTS.md

Guidance for coding agents working in this repository.

## Project

Beatly API — backend for Beatly, a music streaming app. FastAPI service
that aggregates music metadata and audio streams for the mobile and
desktop clients.

Status: early rewrite of a previous, unmaintained backend. Endpoints are
ported one at a time, each rewritten to follow the response contract
below. `routes/`, `services/` and `middlewares/` are still empty.

## Commands

    source venv/bin/activate

    uvicorn app:app --reload     dev server, docs at :8000/docs
    pytest                       all tests
    pytest test/test_contract.py::test_name
    ruff check .
    ruff format .

## Response contract

Every JSON response follows one of two shapes, defined in
`models/responses.py`:

    ApiSuccess[T]  ->  {"ok": true, "data": ...}   via ok_response(data)
    ApiError       ->  {"ok": false, "reason": "..."} via error_response(reason)

| Case                                      | Status |
| ----------------------------------------- | ------ |
| Success                                   | 200    |
| Expected empty state                      | 200 with ok:false |
| Parent resource does not exist            | 404    |
| Invalid input                             | 422    |
| Upstream service failed                   | 502    |
| Upstream timed out                        | 504    |
| Unhandled internal error                  | 500    |

A track that exists but has no lyrics returns 200 with ok:false.
A track id that does not exist returns 404. Clients check `ok` in the
body, not the status code.

## Layering

Routers own all HTTP logic. Services never import HTTPException, never
build responses, never know about status codes. They return domain data
or raise domain exceptions from `core/exceptions.py`.

`AppError` is the base class; each subclass fixes a status code and a
default reason. Raise `NotFound()` or `NotFound("genre_not_found")` and
let the global handlers in `app.py` build the response. Those four
handlers are the only place HTTP responses are constructed from
exceptions.

For a new failure mode, add an `AppError` subclass rather than handling
it inline in a route.

## Hard rules

- Error responses never expose exception messages, stack traces or
  internal details. Those go to the server log only.
- Never `print()`. Use `logging.getLogger(__name__)`.
  `setup_logging()` is called once in `app.py`; never call it again.
- No bare `except`, and no `except` that swallows an error and returns
  an empty value.
- Never use 404 for an expected empty state.

## Layout

    app.py          entrypoint, global exception handlers
    routes/         HTTP layer, one file per domain, wired as APIRouter
    services/       business logic and external providers
    models/         pydantic models
    core/           config, exceptions, logging
    test/           mirrors routes/ and services/
    docs/           workflow, testing, API documentation

## Definition of done

An endpoint is done when it has all four:

1. Response contract applied.
2. Error handling per the rules above.
3. Tests for its three cases: with data, expected empty, parent not found.
   A fourth for upstream failure when it calls an external service.
4. Its entry in `docs/api/`.

Tests and implementation ship in the same branch and the same PR.

## Roles

- Implementation: Claude Code.
- Review: Codex, via `codex review --uncommitted`, before committing.
  Read-only. See the guidelines below.
- Decisions and merge: the repo owner.

## Review guidelines

- Flag any response not following the ok/data or ok/reason envelope as P0.
- Flag any error response exposing an exception message, stack trace or
  internal detail as P0.
- Flag a 404 used for an expected empty state as P0.
- Flag a service importing HTTPException or building an HTTP response
  as P0.
- Flag a bare except, or an except that swallows an error and returns an
  empty value, as P0.
- Flag `print()` used as logging as P1.
- Flag an endpoint missing tests for its expected-empty or not-found
  case as P1.
- Do not flag formatting or lint issues. CI covers those.