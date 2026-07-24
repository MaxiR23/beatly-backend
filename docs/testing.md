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
2. Expected empty state: 200 with ok:false and the right reason.
3. Parent resource not found: 404.

Add a fourth for upstream failure (502) wherever the endpoint calls
an external service.

## File header

Each test file MUST start with this header:

    # test/routes/test_genres.py
    #
    # Tests for the genres endpoints.
    #
    # Tested:
    # - GET /genres/{slug}/playlists returns playlists for a valid genre
    # - Returns 200 with ok:false when the genre has no playlists
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