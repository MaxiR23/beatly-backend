# CLAUDE.md

Guidance for coding agents working in this repository.

## Project

Beatly API — backend for Beatly, a music streaming app. FastAPI service
that aggregates music metadata and audio streams for the mobile and
desktop clients.

Status: early rewrite of a previous, unmaintained backend. Endpoints are
ported one at a time, each rewritten to follow the response contract.

## Commands

    source venv/bin/activate

    uvicorn app:app --reload     dev server, docs at :8000/docs
    pytest                       all tests
    pytest test/test_contract.py::test_name
    ruff check .
    ruff format --check .        what CI runs
    ruff format .                to fix

The local gate is `ruff check .`, `ruff format --check .`, `pytest`.
Hooks: pre-commit runs ruff, pre-push runs pytest. CI runs all three and
is not skippable.

## Response contract

The contract is defined in `docs/api/conventions.md`. Read it before
writing any endpoint.

In code it is implemented by two models in `models/responses.py`:

    ApiSuccess[T]  ->  {"ok": true, "data": ...}      via ok_response(data)
    ApiError       ->  {"ok": false, "reason": "..."} via error_response(reason)

Paginated list endpoints return `Paginated[T]`: `{"items": [...],
"page": {...}}` inside `data`. See the Pagination section of
`conventions.md`.

## Layering

Routers own all HTTP logic. Services never import HTTPException, never
build responses, never know about status codes. They return domain data
or raise domain exceptions from `core/exceptions.py`.

`AppError` is the base class; each subclass fixes a status code and a
default reason. Raise `NotFound()` or `NotFound("genre_not_found")` and
let the global handlers in `app.py` build the response. Those handlers
are the only place HTTP responses are constructed from exceptions.

For a new failure mode, add an `AppError` subclass rather than handling
it inline in a route.

## Hard rules

- Error responses never expose exception messages, stack traces or
  internal details. Those go to the server log only.
- Never `print()`. Use `logging.getLogger(__name__)`. `setup_logging()`
  is called once in `app.py`; never call it again.
- No bare `except`, and no `except` that swallows an error and returns
  an empty value.
- Never use 404 for an expected empty state.
- Wrap database calls and model construction in
  `translate_upstream_errors()` from `core/upstream.py`. Do not write
  try/except for provider failures in services.
- `user_id` always comes from the token, never from the body or the
  query string.
- Any list endpoint that can grow uses the shared helper in
  `core/pagination.py`. Never hand-roll pagination.
- Applied migration files in `db/migrations/` are immutable history and
  are never edited, not even a comment. A database change is always a
  new numbered file. Clarifications go in `db/migrations/README.md`.
  Re-runnable data backfills are the one exception: they live in
  `db/backfills/`, are regenerated and re-applied by hand, and are not
  numbered migrations. `db/backfills/` holds no backfill today; this is
  the convention for whenever the next one is added, not a description
  of its current contents.
- Never run SQL against Supabase from an agent session. Write the `.sql`
  file; the repo owner applies it.

## Layout

    app.py          entrypoint, global exception handlers
    routes/         HTTP layer, one file per domain, wired as APIRouter
    services/       business logic and external providers
    models/         pydantic models
    core/           config, database, exceptions, logging, pagination
    scripts/        one-off scripts run by hand by the repo owner, never imported by the app (empty today; convention for the first one)
    test/           mirrors routes/ and services/; a script under scripts/ gets its mirror under test/scripts/ too, once one exists
    docs/           workflow, testing, API documentation
    db/migrations/  numbered SQL, applied by hand
    db/backfills/   generated data backfills, regenerated and re-applied by hand (empty today; convention for the first one)

## Definition of done

An endpoint is done when it has all four:

1. Response contract applied.
2. Error handling per the rules above.
3. Tests for its cases: with data, expected empty, parent not found
   where a parent exists, and upstream failure when it calls an
   external service.
4. Its entry in `docs/api/`.

Tests and implementation ship in the same branch and the same PR.

## Workflow

Work goes through the agent loop in `.claude/agents/`:

    refine-issue -> plan-issue -> (human approval) -> implement-issue
    -> review-changes -> verify-findings -> implement-issue (fix mode)
    -> ship-issue (prepare) -> (human approval) -> ship-issue (publish)

Issues live in GitHub, read with `gh issue view`. Only `implement-issue`
writes application code; the other five never touch it and write only to
`.claude/loop/`, which is not versioned.

Only `ship-issue` creates branches, commits, pushes and opens or updates
pull requests, and only when the repo owner invokes it. It prepares the
branch, the commit and the pull request draft, then stops: publishing
requires the owner's approval of the draft. Blocking and important
findings are fixed before a pull request is opened; minor ones are the
owner's call. No other agent touches git history or GitHub.

Branches: `w_<YYMMDD>_<type>_<desc>`. Run `date` before naming one.
Commits: conventional commits, single line, no body. The reasoning,
rejected alternatives and risks go in the pull request description.
Pull requests target `main` and close their issue with `Closes #N`.

Decisions and merge: the repo owner.

## Review

These are the repo's hard failures. `review-changes` applies them; they
are listed here because they are project rules, not agent
configuration.

Blocking:

- A response not following the ok/data or ok/reason envelope.
- An error response exposing an exception message, stack trace or
  internal detail.
- A 404 used for an expected empty state.
- A service importing HTTPException or building an HTTP response.
- A bare except, or an except that swallows an error and returns an
  empty value.
- An edit to an already applied migration file.
- `user_id` read from anywhere other than the token.

Important:

- `print()` used as logging.
- An endpoint missing tests for its expected-empty or upstream failure
  cases.
- A growable list endpoint not using the shared pagination helper.
- An endpoint touched without its `docs/api/` entry updated.

Formatting and lint are not flagged. The gate covers those.

## See also

    docs/api/conventions.md   response contract, status codes, reasons
    docs/api/                 per-endpoint documentation
    docs/testing.md           test conventions and file headers
    docs/adr/                 architecture decision records