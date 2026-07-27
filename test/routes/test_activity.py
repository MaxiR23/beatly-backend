# test/routes/test_activity.py
#
# Tests for the plays and recents endpoints.
#
# Tested:
# - POST /plays inserts a play event, track fields going into the
#   metadata column and track_id into its own column
# - played_at is generated server-side, never taken from the client
# - Returns 422 invalid_request when a required track field is missing or
#   artists is empty, without reaching the database
# - A user_id in the request body is never forwarded to the database;
#   only the authenticated user's id is used
# - POST /recents upserts on (user_id, entity_type, entity_id), so
#   re-registering an entity refreshes played_at instead of duplicating
# - Returns 422 invalid_request when entity_type is not one of
#   album|artist|playlist, or entity_id is missing
# - GET /recents returns the 30 most recent entities, newest first
# - Returns 200 with ok:false and reason "no_recents" when the user has
#   no recent activity
# - Recents are never trimmed or deleted; the read only limits
# - Every query is scoped to the authenticated user's id
# - Returns 502/504 when a query fails or times out
# - An unauthenticated request returns 401 unauthorized
#
# What is covered:
# - Happy path, expected empty state, upsert instead of duplicate,
#   server-owned timestamps, invalid input, upstream failure, upstream
#   timeout, user scoping, unauthenticated access
#
# Run with: pytest test/routes/test_activity.py -v
#
# SEE: routes/activity.py, services/activity_service.py

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.auth import get_current_user_id
from core.database import get_db

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"

_ARTIST = {"id": "artist-1", "name": "Some Artist"}

_TRACK_METADATA = {
    "title": "Track One",
    "artists": [_ARTIST],
    "album": "Album One",
    "album_id": "album-1",
    "thumbnail_url": "https://example.com/t1.png",
    "duration_seconds": 210,
}

_PLAY_BODY = {"track_id": "t1", **_TRACK_METADATA}

_ROW_PLAY = {
    "track_id": "t1",
    "metadata": _TRACK_METADATA,
    "played_at": "2026-01-01T00:00:00Z",
}

_RECENT_BODY = {
    "entity_type": "album",
    "entity_id": "album-1",
    "metadata": {"title": "Album One", "thumbnail_url": "https://example.com/a1.png"},
}

_ROW_RECENT = {**_RECENT_BODY, "played_at": "2026-01-02T00:00:00Z"}

_ROW_RECENT_OLDER = {
    "entity_type": "artist",
    "entity_id": "artist-1",
    "metadata": {"title": "Some Artist"},
    "played_at": "2026-01-01T00:00:00Z",
}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _use_db(db):
    app.dependency_overrides[get_db] = lambda: db


def _fake_insert_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.insert.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_upsert_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.upsert.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_recents_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


# --- POST /plays --------------------------------------------------------


def test_log_play_success():
    _use_db(_fake_insert_db(data=[_ROW_PLAY]))
    _use_auth()

    response = client.post("/plays", json=_PLAY_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == _ROW_PLAY


def test_log_play_stores_track_fields_in_metadata_column():
    db = _fake_insert_db(data=[_ROW_PLAY])
    _use_db(db)
    _use_auth()

    response = client.post("/plays", json=_PLAY_BODY)

    assert response.status_code == 200
    db.table.assert_called_once_with("play_events")
    payload = db.table.return_value.insert.call_args[0][0]
    assert payload["track_id"] == "t1"
    assert payload["metadata"] == _TRACK_METADATA


def test_log_play_generates_played_at_server_side():
    db = _fake_insert_db(data=[_ROW_PLAY])
    _use_db(db)
    _use_auth()
    body = {**_PLAY_BODY, "played_at": "1999-01-01T00:00:00Z"}

    response = client.post("/plays", json=body)

    assert response.status_code == 200
    payload = db.table.return_value.insert.call_args[0][0]
    assert payload["played_at"] != "1999-01-01T00:00:00Z"
    assert "played_at" not in payload["metadata"]


def test_log_play_scopes_insert_to_authenticated_user():
    db = _fake_insert_db(data=[_ROW_PLAY])
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.post("/plays", json=_PLAY_BODY)

    assert response.status_code == 200
    payload = db.table.return_value.insert.call_args[0][0]
    assert payload["user_id"] == "other-user-id"


def test_log_play_does_not_accept_user_id_from_body():
    db = _fake_insert_db(data=[_ROW_PLAY])
    _use_db(db)
    _use_auth()
    body = {**_PLAY_BODY, "user_id": "attacker-id"}

    response = client.post("/plays", json=body)

    assert response.status_code == 200
    payload = db.table.return_value.insert.call_args[0][0]
    assert payload["user_id"] == _USER_ID
    assert "user_id" not in payload["metadata"]


@pytest.mark.parametrize(
    "missing_field", ["track_id", "title", "album", "album_id", "thumbnail_url"]
)
def test_log_play_missing_required_field_returns_invalid_request(missing_field):
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {k: v for k, v in _PLAY_BODY.items() if k != missing_field}

    response = client.post("/plays", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_log_play_empty_artists_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_PLAY_BODY, "artists": []}

    response = client.post("/plays", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_log_play_database_failure_returns_upstream_error():
    _use_db(_fake_insert_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post("/plays", json=_PLAY_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_log_play_database_timeout_returns_upstream_timeout():
    _use_db(_fake_insert_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post("/plays", json=_PLAY_BODY)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_log_play_request_returns_unauthorized():
    response = client.post("/plays", json=_PLAY_BODY)

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- POST /recents ------------------------------------------------------


def test_register_recent_success():
    _use_db(_fake_upsert_db(data=[_ROW_RECENT]))
    _use_auth()

    response = client.post("/recents", json=_RECENT_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == _ROW_RECENT


def test_register_recent_upserts_on_user_and_entity():
    db = _fake_upsert_db(data=[_ROW_RECENT])
    _use_db(db)
    _use_auth()

    response = client.post("/recents", json=_RECENT_BODY)

    assert response.status_code == 200
    db.table.assert_called_once_with("recent_activity")
    db.table.return_value.upsert.assert_called_once()
    _, kwargs = db.table.return_value.upsert.call_args
    assert kwargs == {"on_conflict": "user_id,entity_type,entity_id"}
    db.table.return_value.delete.assert_not_called()


def test_register_recent_refreshes_played_at_server_side():
    db = _fake_upsert_db(data=[_ROW_RECENT])
    _use_db(db)
    _use_auth()
    body = {**_RECENT_BODY, "played_at": "1999-01-01T00:00:00Z"}

    response = client.post("/recents", json=body)

    assert response.status_code == 200
    payload = db.table.return_value.upsert.call_args[0][0]
    assert payload["played_at"] != "1999-01-01T00:00:00Z"


def test_register_recent_defaults_metadata_to_empty_object():
    db = _fake_upsert_db(data=[{**_ROW_RECENT, "metadata": {}}])
    _use_db(db)
    _use_auth()
    body = {"entity_type": "playlist", "entity_id": "playlist-1"}

    response = client.post("/recents", json=body)

    assert response.status_code == 200
    payload = db.table.return_value.upsert.call_args[0][0]
    assert payload["metadata"] == {}


def test_register_recent_invalid_entity_type_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_RECENT_BODY, "entity_type": "track"}

    response = client.post("/recents", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


@pytest.mark.parametrize("missing_field", ["entity_type", "entity_id"])
def test_register_recent_missing_required_field_returns_invalid_request(missing_field):
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {k: v for k, v in _RECENT_BODY.items() if k != missing_field}

    response = client.post("/recents", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_register_recent_does_not_accept_user_id_from_body():
    db = _fake_upsert_db(data=[_ROW_RECENT])
    _use_db(db)
    _use_auth()
    body = {**_RECENT_BODY, "user_id": "attacker-id"}

    response = client.post("/recents", json=body)

    assert response.status_code == 200
    payload = db.table.return_value.upsert.call_args[0][0]
    assert payload["user_id"] == _USER_ID


def test_register_recent_database_failure_returns_upstream_error():
    _use_db(_fake_upsert_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post("/recents", json=_RECENT_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_register_recent_database_timeout_returns_upstream_timeout():
    _use_db(_fake_upsert_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post("/recents", json=_RECENT_BODY)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_register_recent_request_returns_unauthorized():
    response = client.post("/recents", json=_RECENT_BODY)

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- GET /recents -------------------------------------------------------


def test_returns_recents_newest_first_limited_to_thirty():
    db = _fake_recents_db(data=[_ROW_RECENT, _ROW_RECENT_OLDER])
    _use_db(db)
    _use_auth()

    response = client.get("/recents")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"items": [_ROW_RECENT, _ROW_RECENT_OLDER]}
    query = db.table.return_value.select.return_value.eq.return_value
    query.order.assert_called_once_with("played_at", desc=True)
    query.order.return_value.limit.assert_called_once_with(30)


def test_empty_recents_returns_no_recents():
    _use_db(_fake_recents_db(data=[]))
    _use_auth()

    response = client.get("/recents")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_recents"}


def test_listing_recents_never_deletes_rows():
    db = _fake_recents_db(data=[_ROW_RECENT])
    _use_db(db)
    _use_auth()

    response = client.get("/recents")

    assert response.status_code == 200
    db.table.return_value.delete.assert_not_called()


def test_recents_scopes_query_to_authenticated_user():
    db = _fake_recents_db(data=[_ROW_RECENT])
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.get("/recents")

    assert response.status_code == 200
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "user_id", "other-user-id"
    )


def test_recents_database_failure_returns_upstream_error():
    _use_db(_fake_recents_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/recents")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_recents_database_timeout_returns_upstream_timeout():
    _use_db(_fake_recents_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/recents")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_recents_request_returns_unauthorized():
    response = client.get("/recents")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
