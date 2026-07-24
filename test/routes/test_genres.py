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
# - GET /genres/{slug}/playlists returns the genre's playlists, ordered
#   by sort_order, with genre_id, sort_order, created_at and
#   updated_at stripped out
# - Returns 404 via NotFound when the slug matches no genre
# - Returns 200 with ok:false and reason "no_playlists" when the genre
#   exists but has no playlists
# - Returns 502/504 when the genre lookup fails or times out
# - Returns 502/504 when the playlists query fails or times out
# - Returns 502 via UpstreamError when a playlist row fails validation
# - GET /genres/{slug}/categories returns the distinct, non-null
#   categories of a genre's playlists, sorted
# - Returns 200 with ok:false and reason "no_categories" when the
#   genre exists but has no non-null categories
# - Returns 502/504 when the categories query fails or times out
# - Returns 502 via UpstreamError when a row is not a mapping
# - Returns 502 via UpstreamError when a category value is not sortable
#   against the others (e.g. a non-string value)
# - GET /genre-playlists/{playlist_id}/tracks returns the playlist's
#   tracks, reordered by position even when the tracks table returns
#   them unordered
# - Returns 404 via NotFound when the playlist_id matches no playlist
# - Returns 200 with ok:false and reason "no_tracks" when the playlist
#   exists but has no rows in genre_playlist_tracks
# - Returns 502/504 when the playlist lookup fails or times out
# - Returns 502/504 when the genre_playlist_tracks query fails or times out
# - Returns 502/504 when the tracks query fails or times out
# - Returns 502 via UpstreamError when a track_id in genre_playlist_tracks
#   has no matching row in tracks (malformed upstream data)
# - Returns 502 via UpstreamError when a track row fails validation
# - Returns 422 invalid_request when playlist_id is not a valid UUID,
#   without reaching the database
#
# What is covered:
# - Happy path, expected empty state, upstream failure, upstream timeout,
#   upstream connection failure, malformed row, non-mapping row, parent
#   not found
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


_GENRE_ID = "11111111-1111-1111-1111-111111111111"


def _fake_playlists_db(
    genre_rows=None, playlist_rows=None, genre_error=None, playlists_error=None
):
    if genre_rows is None:
        genre_rows = [{"id": _GENRE_ID}]

    db = MagicMock()

    def table_side_effect(name):
        table_mock = MagicMock()
        if name == "genres":
            query = table_mock.select.return_value.eq.return_value
            if genre_error is not None:
                query.execute.side_effect = genre_error
            else:
                query.execute.return_value = MagicMock(data=genre_rows)
        elif name == "genre_playlists":
            query = table_mock.select.return_value.eq.return_value.order.return_value
            if playlists_error is not None:
                query.execute.side_effect = playlists_error
            else:
                query.execute.return_value = MagicMock(data=playlist_rows)
        return table_mock

    db.table.side_effect = table_side_effect
    return db


_CATEGORIES_GENRE_ID = "11111111-1111-1111-1111-111111111111"


def _fake_categories_db(
    genre_rows=None, playlist_rows=None, genre_error=None, playlists_error=None
):
    if genre_rows is None:
        genre_rows = [{"id": _CATEGORIES_GENRE_ID}]

    db = MagicMock()

    def table_side_effect(name):
        table_mock = MagicMock()
        if name == "genres":
            query = table_mock.select.return_value.eq.return_value
            if genre_error is not None:
                query.execute.side_effect = genre_error
            else:
                query.execute.return_value = MagicMock(data=genre_rows)
        elif name == "genre_playlists":
            query = table_mock.select.return_value.eq.return_value
            if playlists_error is not None:
                query.execute.side_effect = playlists_error
            else:
                query.execute.return_value = MagicMock(data=playlist_rows)
        return table_mock

    db.table.side_effect = table_side_effect
    return db


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


def test_returns_genre_playlists_ordered_by_sort_order():
    rows = [
        {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "genre_id": _GENRE_ID,
            "title": "Rock Anthems",
            "description": "Loud guitars",
            "thumbnail_url": "https://example.com/rock.png",
            "sort_order": 1,
            "track_count": 25,
            "category": "mood",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "genre_id": _GENRE_ID,
            "title": "Deep Cuts",
            "description": None,
            "thumbnail_url": None,
            "sort_order": 2,
            "track_count": 10,
            "category": None,
            "created_at": "2026-01-02T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
    ]
    _use_db(_fake_playlists_db(playlist_rows=rows))

    response = client.get("/genres/rock/playlists")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "playlists": [
            {
                "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "title": "Rock Anthems",
                "description": "Loud guitars",
                "thumbnail_url": "https://example.com/rock.png",
                "track_count": 25,
                "category": "mood",
            },
            {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "title": "Deep Cuts",
                "description": None,
                "thumbnail_url": None,
                "track_count": 10,
                "category": None,
            },
        ]
    }


def test_unknown_slug_returns_genre_not_found():
    _use_db(_fake_playlists_db(genre_rows=[]))

    response = client.get("/genres/unknown/playlists")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "genre_not_found"}


def test_genre_with_no_playlists_returns_no_playlists():
    _use_db(_fake_playlists_db(playlist_rows=[]))

    response = client.get("/genres/rock/playlists")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_playlists"}


def test_genre_lookup_failure_returns_upstream_error():
    _use_db(_fake_playlists_db(genre_error=APIError({"message": "connection refused"})))

    response = client.get("/genres/rock/playlists")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_genre_lookup_timeout_returns_upstream_timeout():
    _use_db(_fake_playlists_db(genre_error=httpx.ReadTimeout("timed out")))

    response = client.get("/genres/rock/playlists")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_playlists_query_failure_returns_upstream_error():
    _use_db(
        _fake_playlists_db(playlists_error=APIError({"message": "connection refused"}))
    )

    response = client.get("/genres/rock/playlists")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_playlists_query_timeout_returns_upstream_timeout():
    _use_db(_fake_playlists_db(playlists_error=httpx.ReadTimeout("timed out")))

    response = client.get("/genres/rock/playlists")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_malformed_playlist_row_returns_upstream_error():
    rows = [{"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "title": "Rock Anthems"}]
    _use_db(_fake_playlists_db(playlist_rows=rows))

    response = client.get("/genres/rock/playlists")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_returns_distinct_sorted_categories():
    rows = [
        {"category": "workout"},
        {"category": "chill"},
        {"category": "workout"},
        {"category": None},
    ]
    _use_db(_fake_categories_db(playlist_rows=rows))

    response = client.get("/genres/rock/categories")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"categories": ["chill", "workout"]}


def test_unknown_slug_returns_genre_not_found_for_categories():
    _use_db(_fake_categories_db(genre_rows=[]))

    response = client.get("/genres/unknown/categories")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "genre_not_found"}


def test_genre_with_no_categories_returns_no_categories():
    _use_db(_fake_categories_db(playlist_rows=[]))

    response = client.get("/genres/rock/categories")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_categories"}


def test_genre_with_only_null_categories_returns_no_categories():
    rows = [{"category": None}, {"category": None}]
    _use_db(_fake_categories_db(playlist_rows=rows))

    response = client.get("/genres/rock/categories")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_categories"}


def test_genre_lookup_failure_returns_upstream_error_for_categories():
    _use_db(
        _fake_categories_db(genre_error=APIError({"message": "connection refused"}))
    )

    response = client.get("/genres/rock/categories")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_genre_lookup_timeout_returns_upstream_timeout_for_categories():
    _use_db(_fake_categories_db(genre_error=httpx.ReadTimeout("timed out")))

    response = client.get("/genres/rock/categories")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_categories_query_failure_returns_upstream_error():
    _use_db(
        _fake_categories_db(playlists_error=APIError({"message": "connection refused"}))
    )

    response = client.get("/genres/rock/categories")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_categories_query_timeout_returns_upstream_timeout():
    _use_db(_fake_categories_db(playlists_error=httpx.ReadTimeout("timed out")))

    response = client.get("/genres/rock/categories")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_non_mapping_playlist_row_returns_upstream_error():
    _use_db(_fake_categories_db(playlist_rows=[None]))

    response = client.get("/genres/rock/categories")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_malformed_category_value_returns_upstream_error():
    rows = [{"category": "chill"}, {"category": 42}]
    _use_db(_fake_categories_db(playlist_rows=rows))

    response = client.get("/genres/rock/categories")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


_PLAYLIST_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _fake_tracks_db(
    playlist_rows=None,
    playlist_track_rows=None,
    track_rows=None,
    playlist_error=None,
    playlist_tracks_error=None,
    tracks_error=None,
):
    if playlist_rows is None:
        playlist_rows = [{"id": _PLAYLIST_ID}]

    db = MagicMock()

    def table_side_effect(name):
        table_mock = MagicMock()
        if name == "genre_playlists":
            query = table_mock.select.return_value.eq.return_value
            if playlist_error is not None:
                query.execute.side_effect = playlist_error
            else:
                query.execute.return_value = MagicMock(data=playlist_rows)
        elif name == "genre_playlist_tracks":
            query = table_mock.select.return_value.eq.return_value.order.return_value.limit.return_value
            if playlist_tracks_error is not None:
                query.execute.side_effect = playlist_tracks_error
            else:
                query.execute.return_value = MagicMock(data=playlist_track_rows)
        elif name == "tracks":
            query = table_mock.select.return_value.in_.return_value
            if tracks_error is not None:
                query.execute.side_effect = tracks_error
            else:
                query.execute.return_value = MagicMock(data=track_rows)
        return table_mock

    db.table.side_effect = table_side_effect
    return db


def test_returns_playlist_tracks_reordered_by_position():
    playlist_track_rows = [
        {"track_id": "t1", "position": 1},
        {"track_id": "t2", "position": 2},
    ]
    # Returned out of position order, on purpose, to verify reordering.
    track_rows = [
        {
            "track_id": "t2",
            "title": "Song B",
            "artists": [{"id": "a2", "name": "Artist Two"}],
            "album": "Album B",
            "album_id": "album-b",
            "duration_seconds": 200,
            "thumbnail_url": "https://example.com/b.png",
        },
        {
            "track_id": "t1",
            "title": "Song A",
            "artists": [{"id": "a1", "name": "Artist One"}],
            "album": "Album A",
            "album_id": "album-a",
            "duration_seconds": 180,
            "thumbnail_url": "https://example.com/a.png",
        },
    ]
    _use_db(
        _fake_tracks_db(
            playlist_track_rows=playlist_track_rows,
            track_rows=track_rows,
        )
    )

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "tracks": [
            {
                "track_id": "t1",
                "title": "Song A",
                "artists": [{"id": "a1", "name": "Artist One"}],
                "album": "Album A",
                "album_id": "album-a",
                "duration_seconds": 180,
                "thumbnail_url": "https://example.com/a.png",
                "position": 1,
            },
            {
                "track_id": "t2",
                "title": "Song B",
                "artists": [{"id": "a2", "name": "Artist Two"}],
                "album": "Album B",
                "album_id": "album-b",
                "duration_seconds": 200,
                "thumbnail_url": "https://example.com/b.png",
                "position": 2,
            },
        ]
    }


def test_unknown_playlist_id_returns_playlist_not_found():
    _use_db(_fake_tracks_db(playlist_rows=[]))

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_playlist_with_no_tracks_returns_no_tracks():
    _use_db(_fake_tracks_db(playlist_track_rows=[]))

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_tracks"}


def test_playlist_lookup_failure_returns_upstream_error():
    _use_db(_fake_tracks_db(playlist_error=APIError({"message": "connection refused"})))

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_playlist_lookup_timeout_returns_upstream_timeout():
    _use_db(_fake_tracks_db(playlist_error=httpx.ReadTimeout("timed out")))

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_playlist_tracks_query_failure_returns_upstream_error():
    _use_db(
        _fake_tracks_db(
            playlist_tracks_error=APIError({"message": "connection refused"})
        )
    )

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_playlist_tracks_query_timeout_returns_upstream_timeout():
    _use_db(_fake_tracks_db(playlist_tracks_error=httpx.ReadTimeout("timed out")))

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_tracks_query_failure_returns_upstream_error():
    _use_db(
        _fake_tracks_db(
            playlist_track_rows=[{"track_id": "t1", "position": 1}],
            tracks_error=APIError({"message": "connection refused"}),
        )
    )

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_tracks_query_timeout_returns_upstream_timeout():
    _use_db(
        _fake_tracks_db(
            playlist_track_rows=[{"track_id": "t1", "position": 1}],
            tracks_error=httpx.ReadTimeout("timed out"),
        )
    )

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_track_missing_from_tracks_table_returns_upstream_error():
    _use_db(
        _fake_tracks_db(
            playlist_track_rows=[
                {"track_id": "t1", "position": 1},
                {"track_id": "t2", "position": 2},
            ],
            track_rows=[
                {
                    "track_id": "t1",
                    "title": "Song A",
                    "artists": [{"id": "a1", "name": "Artist One"}],
                    "album": "Album A",
                    "album_id": "album-a",
                    "duration_seconds": 180,
                    "thumbnail_url": "https://example.com/a.png",
                }
            ],
        )
    )

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_malformed_track_row_returns_upstream_error():
    _use_db(
        _fake_tracks_db(
            playlist_track_rows=[{"track_id": "t1", "position": 1}],
            track_rows=[{"track_id": "t1", "title": "Song A"}],
        )
    )

    response = client.get(f"/genre-playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_malformed_playlist_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)

    response = client.get("/genre-playlists/not-a-uuid/tracks")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()
