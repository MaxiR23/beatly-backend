# test/routes/test_genres.py
#
# Tests for the genres endpoints.
#
# Tested:
# - GET /genres returns the genre list, ordered by sort_order, with
#   id, sort_order and created_at stripped out
# - Returns 200 with ok:false and reason "no_genres" when the table
#   is empty
# - Returns 502 via UpstreamError when the database call fails
# - Returns 504 via UpstreamTimeout when the database call times out
# - Returns 502 via UpstreamError when the database is unreachable
# - Returns 502 via UpstreamError when a row fails validation
# - Returns 502 via UpstreamError when a row is not a mapping
#
# What is covered:
# - Happy path, expected empty state, upstream failure, upstream timeout,
#   upstream connection failure, malformed row, non-mapping row
#
# Run with: pytest test/routes/test_genres.py -v
#
# SEE: routes/genres.py, services/genre_service.py

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.database import get_db

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clear_db_override():
    yield
    app.dependency_overrides.pop(get_db, None)


def _fake_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.select.return_value.order.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _use_db(db):
    app.dependency_overrides[get_db] = lambda: db


def test_returns_genre_list_ordered_by_sort_order():
    rows = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "slug": "rock",
            "name": "Rock",
            "description": "Guitar-driven music",
            "sort_order": 1,
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "slug": "jazz",
            "name": "Jazz",
            "description": None,
            "sort_order": 2,
            "created_at": "2026-01-02T00:00:00Z",
        },
    ]
    _use_db(_fake_db(data=rows))

    response = client.get("/genres")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "genres": [
            {"slug": "rock", "name": "Rock", "description": "Guitar-driven music"},
            {"slug": "jazz", "name": "Jazz", "description": None},
        ]
    }


def test_empty_table_returns_no_genres():
    _use_db(_fake_db(data=[]))

    response = client.get("/genres")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_genres"}


def test_database_failure_returns_upstream_error():
    _use_db(_fake_db(error=APIError({"message": "connection refused"})))

    response = client.get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_database_timeout_returns_upstream_timeout():
    _use_db(_fake_db(error=httpx.ReadTimeout("timed out")))

    response = client.get("/genres")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_database_unreachable_returns_upstream_error():
    _use_db(_fake_db(error=httpx.ConnectError("connection refused")))

    response = client.get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_malformed_row_returns_upstream_error():
    rows = [{"slug": "rock", "description": "Guitar-driven music"}]
    _use_db(_fake_db(data=rows))

    response = client.get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_non_mapping_row_returns_upstream_error():
    _use_db(_fake_db(data=[None]))

    response = client.get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
