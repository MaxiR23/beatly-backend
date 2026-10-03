# Testing

Conventions for tests in this repo.

## Commands

    pytest              run the mock tests (integration tests are
                        deselected)
    pytest -m integration   run the integration tests (needs the local
                            Supabase stack, see "Integration tests")
    pytest -v           verbose
    pytest --cov        with coverage report

## File location

Tests live in `test/` at the project root, mirroring the source
structure:

    routes/genres.py       -> test/routes/test_genres.py
    services/genre_repo.py -> test/services/test_genre_repo.py
    core/exceptions.py     -> test/core/test_exceptions.py

Integration tests live in `test/integration/`, one file per domain, named
`test_<domain>_integration.py`. The suffix is not decoration: `test/` has an
`__init__.py` but its subdirectories do not, so pytest names each module by
its basename and `test/integration/test_likes.py` would collide with
`test/routes/test_likes.py`.

Cross-cutting tests that do not map to a single module live at the
root of `test/`, for example `test/test_contract.py`.

## Naming

Test names describe the behavior being verified, not a category:

    def test_success_response_omits_reason():
    def test_unknown_route_returns_contract_shape():
    def test_unhandled_exception_does_not_leak_details():

The name should be enough to know what broke when it fails.

## Coverage per endpoint

Every ported endpoint ships with at least three tests:

1. Success with data.
2. Expected empty state:
   - Paginated list endpoints: 200 with ok:true, `items: []`,
     `has_more: false`, `next_cursor: null` and `total: 0`.
   - Everything else: 200 with ok:false and the right reason.
3. Parent resource not found: 404.

Add a fourth for upstream failure (502) wherever the endpoint calls
an external service.

On top of the mocked ones, every route that touches the database has at
least one integration test.

Paginated endpoints ship four more: a first page with `has_more: true`
and `total` present, a second page fetched with the returned cursor
where `total` is null, the last page, and an invalid cursor returning
422 `invalid_cursor` without touching the database.

## File header

Each test file MUST start with this header:

    # test/routes/test_genres.py
    #
    # Tests for the genres endpoints.
    #
    # Tested:
    # - GET /genres/{slug}/playlists returns playlists for a valid genre
    # - Returns an empty first page when the genre has no playlists
    # - Returns 404 when the slug does not exist
    #
    # What is covered:
    # - Happy path, expected empty state, missing parent resource
    #
    # Run with: pytest test/routes/test_genres.py -v
    #
    # SEE: routes/genres.py, services/genre_repo.py

This replaces the single-line `# INFO:` rule for test files. The
single-line rule still applies to non-test code.

## Mocking external services

The default tests (`test/routes/`, `test/core/`) mock all HTTP, database
and external-provider calls. They never hit a real service, not even a
sandbox. The tests in `test/integration/` are the one exception: they hit
only the local Supabase stack, never a remote service.

Use `unittest.mock` for internal boundaries and `respx` or
`responses` for HTTP. Configure mocks so an unhandled request fails
instead of passing through, otherwise a test can silently reach a
real endpoint.

Supabase is mocked by overriding a dependency with a `MagicMock` that
models the postgrest call chain, as in `test/routes/test_likes.py`. Which
dependency depends on the domain: the six user-data domains (likes,
library, playlists, activity, bug reports, profile) and `core/auth.py`
query as the caller, so they override `get_user_db`; genres and public
query the catalog, so they override `get_db`. `POST
/playlists/{id}/tracks` and `.../tracks/bulk` are the exception: the
catalog upsert inside them runs on `get_db` (service-role) while the
rest of the request runs on `get_user_db`. `test/routes/test_playlists.py`'s
`_use_db` helper points both overrides at the same mock, so the tests
written before that split still assert against one mock as before;
`test_add_track_writes_the_catalog_with_the_service_role_client` and
`test_bulk_add_writes_the_catalog_with_the_service_role_client` are the
two tests that override `get_user_db` and `get_db` with separate mocks,
to prove the upsert reaches `get_db` and nothing else does. When the
chain changes, the fake changes with it: a `MagicMock` accepts any call,
so an assertion on the exact arguments is the only thing that actually
pins behaviour.

## Integration tests

They run the real app (`TestClient`, no dependency overrides) against a
local Supabase stack built only from `db/migrations`. They prove what a mock
cannot: that the queries, embeds and RPCs match the schema the migrations
build, and that RLS lets the caller do what the route does.

An integration test is needed for every route that reads or writes the
database (at least one each), and every change to a query, to an RPC call or
a new migration ships with its own. A mocked test is enough for pure logic,
error branches and upstream failure or timeout, which the integration cannot
provoke.

They are deselected by default (`addopts` carries `-m 'not integration'`),
so `pytest` needs no Docker. To run them locally, ideally in a separate
terminal because the `eval` leaves the variables exported:

    supabase --workdir db/local start
    bash db/local/apply_migrations.sh
    eval "$(supabase --workdir db/local status -o env \
      --override-name api.url=SUPABASE_URL \
      --override-name auth.anon_key=SUPABASE_ANON_KEY \
      --override-name auth.service_role_key=SUPABASE_SERVICE_ROLE_KEY \
      --override-name auth.jwt_secret=SUPABASE_JWT_SECRET \
      | grep '^SUPABASE_' | sed 's/^/export /')"
    pytest -m integration
    supabase --workdir db/local stop --no-backup

The `SUPABASE_*` variables come from the process environment, which wins
over `.env`. `test/integration/conftest.py` aborts the whole session if
`SUPABASE_URL` does not point at `127.0.0.1` or `localhost`, so a production
`.env` can never be hit by mistake. If you export the variables and then run
`uvicorn` in the same terminal, the app points at the local stack too.

Isolation is by data: a new user per test and unique ids, with no teardown.
In CI the database is new on every run; locally, to start from zero run
`stop --no-backup` and then `start` and the script again.

Docker Desktop's VM clock can lag the host by minutes. A test that needs a
`since` takes it from a timestamp the API returned (the database's) or uses a
fixed far date, never `datetime.now()` of the host.

## TDD workflow

Red, green, refactor:

1. Write the test. It fails.
2. Write the minimum code to make it pass.
3. Refactor with the tests green.

Tests and implementation ship in the same branch and the same PR.
An endpoint without tests is not done.

## SEE

- pytest docs: https://docs.pytest.org/
- FastAPI testing: https://fastapi.tiangolo.com/tutorial/testing/