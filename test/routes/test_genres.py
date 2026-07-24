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

from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app

client = TestClient(app, raise_server_exceptions=False)


def _mock_query(data=None, error=None):
    supabase = MagicMock()
    query = supabase.table.return_value.select.return_value.order.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return supabase


@patch("services.genre_service.get_supabase")
def test_returns_genre_list_ordered_by_sort_order(mock_get_supabase):
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
    mock_get_supabase.return_value = _mock_query(data=rows)

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


@patch("services.genre_service.get_supabase")
def test_empty_table_returns_no_genres(mock_get_supabase):
    mock_get_supabase.return_value = _mock_query(data=[])

    response = client.get("/genres")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_genres"}


@patch("services.genre_service.get_supabase")
def test_database_failure_returns_upstream_error(mock_get_supabase):
    mock_get_supabase.return_value = _mock_query(
        error=APIError({"message": "connection refused"})
    )

    response = client.get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@patch("services.genre_service.get_supabase")
def test_database_timeout_returns_upstream_timeout(mock_get_supabase):
    mock_get_supabase.return_value = _mock_query(error=httpx.ReadTimeout("timed out"))

    response = client.get("/genres")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


@patch("services.genre_service.get_supabase")
def test_database_unreachable_returns_upstream_error(mock_get_supabase):
    mock_get_supabase.return_value = _mock_query(
        error=httpx.ConnectError("connection refused")
    )

    response = client.get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@patch("services.genre_service.get_supabase")
def test_malformed_row_returns_upstream_error(mock_get_supabase):
    rows = [{"slug": "rock", "description": "Guitar-driven music"}]
    mock_get_supabase.return_value = _mock_query(data=rows)

    response = client.get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@patch("services.genre_service.get_supabase")
def test_non_mapping_row_returns_upstream_error(mock_get_supabase):
    mock_get_supabase.return_value = _mock_query(data=[None])

    response = client.get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
