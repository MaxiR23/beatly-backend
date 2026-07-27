# test/routes/test_likes.py
#
# Tests for the likes endpoints.
#
# Tested:
# - GET /likes returns the user's active likes, ordered by created_at
# - Returns 200 with ok:false and reason "no_likes" when the user has no
#   active likes
# - POST /likes upserts a like, keyed on (user_id, track_id), always
#   clearing deleted_at so re-liking a soft-deleted row revives it
# - Returns 422 invalid_request when a required field is missing or
#   artists is empty, without reaching the database
# - A user_id in the request body is never forwarded to the database;
#   only the authenticated user's id is used
# - DELETE /likes/{track_id} soft-deletes a like by setting deleted_at
# - Unliking a track that isn't liked is still 200 ok:true (idempotent)
# - GET /likes/sync returns active and soft-deleted rows changed since
#   `since`, ordered by updated_at
# - Returns 422 invalid_request when `since` is missing or malformed,
#   without reaching the database
# - A sync with no changes is 200 ok:true with an empty list, not
#   "no_likes"
# - Every query is scoped to the authenticated user's id
# - Returns 502/504 when a query fails or times out
# - An unauthenticated request returns 401 unauthorized
#
# What is covered:
# - Happy path, expected empty state, revival on re-like, idempotent
#   unlike, invalid input, upstream failure, upstream timeout, user
#   scoping, unauthenticated access
#
# Run with: pytest test/routes/test_likes.py -v
#
# SEE: routes/likes.py, services/likes_service.py

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

_ROW_LIKE = {
    "track_id": "t1",
    "title": "Track One",
    "artists": [_ARTIST],
    "album": "Album One",
    "album_id": "album-1",
    "thumbnail_url": "https://example.com/t1.png",
    "duration_seconds": 210,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "deleted_at": None,
}

_ROW_LIKE_DELETED = {
    **_ROW_LIKE,
    "track_id": "t2",
    "deleted_at": "2026-01-03T00:00:00Z",
    "updated_at": "2026-01-03T00:00:00Z",
}

_ADD_BODY = {
    "track_id": "t1",
    "title": "Track One",
    "artists": [_ARTIST],
    "album": "Album One",
    "album_id": "album-1",
    "thumbnail_url": "https://example.com/t1.png",
    "duration_seconds": 210,
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


def _fake_list_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.select.return_value.eq.return_value.is_.return_value.order.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_sync_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.select.return_value.eq.return_value.gt.return_value.order.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_like_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.upsert.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_unlike_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.update.return_value.eq.return_value.eq.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


# --- GET /likes -------------------------------------------------------


def test_returns_active_likes_ordered_by_created_at():
    db = _fake_list_db(data=[_ROW_LIKE])
    _use_db(db)
    _use_auth()

    response = client.get("/likes")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"likes": [_ROW_LIKE]}
    query = db.table.return_value.select.return_value.eq.return_value
    query.is_.assert_called_once_with("deleted_at", "null")
    query.is_.return_value.order.assert_called_once_with("created_at")


def test_empty_likes_returns_no_likes():
    _use_db(_fake_list_db(data=[]))
    _use_auth()

    response = client.get("/likes")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_likes"}


def test_list_scopes_query_to_authenticated_user():
    db = _fake_list_db(data=[_ROW_LIKE])
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.get("/likes")

    assert response.status_code == 200
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "user_id", "other-user-id"
    )


def test_list_database_failure_returns_upstream_error():
    _use_db(_fake_list_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/likes")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_list_database_timeout_returns_upstream_timeout():
    _use_db(_fake_list_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/likes")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_list_request_returns_unauthorized():
    response = client.get("/likes")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- POST /likes --------------------------------------------------------


def test_like_track_success():
    row = {**_ROW_LIKE}
    db = _fake_like_db(data=[row])
    _use_db(db)
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == row


def test_like_track_upsert_always_clears_deleted_at_to_revive():
    db = _fake_like_db(data=[_ROW_LIKE])
    _use_db(db)
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 200
    db.table.return_value.upsert.assert_called_once_with(
        {**_ADD_BODY, "user_id": _USER_ID, "deleted_at": None},
        on_conflict="user_id,track_id",
    )


@pytest.mark.parametrize(
    "missing_field", ["track_id", "title", "album", "album_id", "thumbnail_url"]
)
def test_like_track_missing_required_field_returns_invalid_request(missing_field):
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {k: v for k, v in _ADD_BODY.items() if k != missing_field}

    response = client.post("/likes", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_like_track_empty_artists_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_ADD_BODY, "artists": []}

    response = client.post("/likes", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_like_track_does_not_accept_user_id_from_body():
    db = _fake_like_db(data=[_ROW_LIKE])
    _use_db(db)
    _use_auth()
    body = {**_ADD_BODY, "user_id": "attacker-id"}

    response = client.post("/likes", json=body)

    assert response.status_code == 200
    called_payload = db.table.return_value.upsert.call_args[0][0]
    assert called_payload["user_id"] == _USER_ID


def test_like_track_upstream_failure_returns_upstream_error():
    _use_db(_fake_like_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_like_track_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_like_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_like_request_returns_unauthorized():
    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- DELETE /likes/{track_id} -------------------------------------------


def test_unlike_track_success():
    _use_db(_fake_unlike_db(data=[_ROW_LIKE]))
    _use_auth()

    response = client.delete("/likes/t1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}


def test_unlike_track_not_liked_is_idempotent():
    _use_db(_fake_unlike_db(data=[]))
    _use_auth()

    response = client.delete("/likes/t1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}


def test_unlike_track_scopes_delete_to_authenticated_user():
    db = _fake_unlike_db(data=[_ROW_LIKE])
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.delete("/likes/t1")

    assert response.status_code == 200
    db.table.return_value.update.return_value.eq.assert_called_once_with(
        "user_id", "other-user-id"
    )


def test_unlike_track_upstream_failure_returns_upstream_error():
    _use_db(_fake_unlike_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.delete("/likes/t1")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_unlike_track_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_unlike_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.delete("/likes/t1")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_unlike_request_returns_unauthorized():
    response = client.delete("/likes/t1")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- GET /likes/sync ------------------------------------------------------


def test_sync_returns_changes_since_ordered_by_updated_at():
    db = _fake_sync_db(data=[_ROW_LIKE, _ROW_LIKE_DELETED])
    _use_db(db)
    _use_auth()

    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"likes": [_ROW_LIKE, _ROW_LIKE_DELETED]}
    query = db.table.return_value.select.return_value.eq.return_value
    query.gt.assert_called_once_with("updated_at", "2026-01-01T00:00:00+00:00")
    query.gt.return_value.order.assert_called_once_with("updated_at")


def test_sync_missing_since_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/likes/sync")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_sync_malformed_since_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/likes/sync", params={"since": "not-a-timestamp"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_sync_no_changes_returns_empty_list_not_no_likes():
    _use_db(_fake_sync_db(data=[]))
    _use_auth()

    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"likes": []}}


def test_sync_scopes_query_to_authenticated_user():
    db = _fake_sync_db(data=[_ROW_LIKE])
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 200
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "user_id", "other-user-id"
    )


def test_sync_database_failure_returns_upstream_error():
    _use_db(_fake_sync_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_sync_database_timeout_returns_upstream_timeout():
    _use_db(_fake_sync_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_sync_request_returns_unauthorized():
    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
