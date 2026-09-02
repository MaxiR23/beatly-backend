# Testing

Conventions for tests in this repo.

## Commands

    pytest              run all tests
    pytest -v           verbose
    pytest --cov        with coverage report

## File location

Tests live in `test/` at the project root, mirroring the source
structure:

    routes/genres.py       -> test/routes/test_genres.py
    services/genre_repo.py -> test/services/test_genre_repo.py
    core/exceptions.py     -> test/core/test_exceptions.py

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
    # - Returns 200 with ok:false when the genre has no playlists
    #   (paginated endpoints return an empty first page instead)
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

All HTTP, database and external-provider calls are mocked. Tests
never hit a real service, not even a sandbox.

Use `unittest.mock` for internal boundaries and `respx` or
`responses` for HTTP. Configure mocks so an unhandled request fails
instead of passing through, otherwise a test can silently reach a
real endpoint.

Supabase is mocked by overriding the `get_db` dependency with a
`MagicMock` that models the postgrest call chain, as in
`test/routes/test_likes.py`. When the chain changes, the fake changes
with it: a `MagicMock` accepts any call, so an assertion on the exact
arguments is the only thing that actually pins behaviour.

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