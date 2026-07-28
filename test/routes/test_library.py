# test/routes/test_library.py
#
# Tests for the library endpoints.
#
# Tested:
# - GET /library returns the user's items, newest added first by default
# - GET /library?sort=title&order=asc returns items alphabetically by title
# - GET /library?sort=added_at&order=desc returns newest first, explicitly
# - Returns 200 with ok:false and reason "no_library_items" when the
#   user's library is empty
# - Returns 422 invalid_request when sort or order is not a valid value,
#   without reaching the database
# - Every list query is scoped to the authenticated user's id
# - Returns 502/504 when the list query fails or times out
# - POST /library adds an item via upsert, keyed on
#   (user_id, kind, external_id)
# - Re-adding an existing item succeeds (upsert), not a 409 conflict
# - Returns 422 invalid_request when a required field is missing or kind
#   is not album/playlist, without reaching the database
# - A user_id in the request body is never forwarded to the database;
#   only the authenticated user's id is used
# - Returns 502/504 when the add query fails or times out
# - Returns 502 when the upsert returns no row
# - DELETE /library/{kind}/{external_id} removes a matching item
# - Returns 404 library_item_not_found when no item matches
# - Every delete query is scoped to the authenticated user's id
# - Returns 422 invalid_request when kind is not album/playlist, without
#   reaching the database
# - Returns 502/504 when the delete query fails or times out
# - An unauthenticated request returns 401 unauthorized
#
# What is covered:
# - Happy path, expected empty state, both sort orders, invalid input,
#   not found, upstream failure, upstream timeout, user scoping,
#   unauthenticated access
#
# Run with: pytest test/routes/test_library.py -v
#
# SEE: routes/library.py, services/library_service.py

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

_ROW_ALPHA = {
    "kind": "playlist",
    "external_id": "p1",
    "title": "Alpha Mix",
    "thumbnail_url": "https://example.com/alpha.png",
    "artist": None,
    "artist_id": None,
    "album_id": None,
    "album_name": None,
    "source": "spotify",
    "added_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}

_ROW_ZETA = {
    "kind": "album",
    "external_id": "a1",
    "title": "Zeta Album",
    "thumbnail_url": "https://example.com/zeta.png",
    "artist": "Some Artist",
    "artist_id": "artist-1",
    "album_id": "album-1",
    "album_name": "Zeta Album",
    "source": "spotify",
    "added_at": "2026-01-02T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
}

_ADD_BODY = {
    "kind": "album",
    "external_id": "a1",
    "title": "Zeta Album",
    "source": "spotify",
    "thumbnail_url": "https://example.com/zeta.png",
    "artist": "Some Artist",
    "artist_id": "artist-1",
    "album_id": "album-1",
    "album_name": "Zeta Album",
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
    query = db.table.return_value.select.return_value.eq.return_value.order.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_add_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.upsert.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_delete_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.delete.return_value.eq.return_value.eq.return_value.eq.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def test_returns_library_items_default_sort_added_at_desc():
    db = _fake_list_db(data=[_ROW_ZETA, _ROW_ALPHA])
    _use_db(db)
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"items": [_ROW_ZETA, _ROW_ALPHA]}
    db.table.return_value.select.return_value.eq.return_value.order.assert_called_once_with(
        "added_at", desc=True
    )


def test_returns_library_items_sorted_by_title_asc():
    db = _fake_list_db(data=[_ROW_ALPHA, _ROW_ZETA])
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"sort": "title", "order": "asc"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"items": [_ROW_ALPHA, _ROW_ZETA]}
    db.table.return_value.select.return_value.eq.return_value.order.assert_called_once_with(
        "title", desc=False
    )


def test_returns_library_items_sorted_by_added_at_desc_explicit():
    db = _fake_list_db(data=[_ROW_ZETA, _ROW_ALPHA])
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"sort": "added_at", "order": "desc"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"items": [_ROW_ZETA, _ROW_ALPHA]}
    db.table.return_value.select.return_value.eq.return_value.order.assert_called_once_with(
        "added_at", desc=True
    )


def test_empty_library_returns_no_library_items():
    _use_db(_fake_list_db(data=[]))
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_library_items"}


def test_invalid_sort_value_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"sort": "bogus"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_invalid_order_value_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"order": "bogus"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_list_database_failure_returns_upstream_error():
    _use_db(_fake_list_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_list_database_timeout_returns_upstream_timeout():
    _use_db(_fake_list_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_list_database_unreachable_returns_upstream_error():
    _use_db(_fake_list_db(error=httpx.ConnectError("connection refused")))
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_list_scopes_query_to_authenticated_user():
    db = _fake_list_db(data=[_ROW_ZETA])
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.get("/library")

    assert response.status_code == 200
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "user_id", "other-user-id"
    )


def test_add_library_item_success():
    row = {
        **_ADD_BODY,
        "added_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    db = _fake_add_db(data=[row])
    _use_db(db)
    _use_auth()

    response = client.post("/library", json=_ADD_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == row
    db.table.return_value.upsert.assert_called_once_with(
        {**_ADD_BODY, "user_id": _USER_ID}, on_conflict="user_id,kind,external_id"
    )


def test_add_library_item_reupsert_is_idempotent():
    row = {
        **_ADD_BODY,
        "added_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    db = _fake_add_db(data=[row])
    _use_db(db)
    _use_auth()

    first = client.post("/library", json=_ADD_BODY)
    second = client.post("/library", json=_ADD_BODY)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["ok"] is True
    assert second.json()["ok"] is True
    assert db.table.return_value.upsert.call_count == 2


def test_add_library_item_missing_required_field_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {k: v for k, v in _ADD_BODY.items() if k != "title"}

    response = client.post("/library", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_add_library_item_invalid_kind_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_ADD_BODY, "kind": "song"}

    response = client.post("/library", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_add_library_item_without_returned_row_returns_upstream_error():
    _use_db(_fake_add_db(data=[]))
    _use_auth()

    response = client.post("/library", json=_ADD_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_add_library_item_upstream_failure_returns_upstream_error():
    _use_db(_fake_add_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post("/library", json=_ADD_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_add_library_item_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_add_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post("/library", json=_ADD_BODY)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_add_library_item_does_not_accept_user_id_from_body():
    row = {
        **_ADD_BODY,
        "added_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    db = _fake_add_db(data=[row])
    _use_db(db)
    _use_auth()
    body = {**_ADD_BODY, "user_id": "attacker-id"}

    response = client.post("/library", json=body)

    assert response.status_code == 200
    called_payload = db.table.return_value.upsert.call_args[0][0]
    assert called_payload["user_id"] == _USER_ID


def test_remove_library_item_success():
    _use_db(_fake_delete_db(data=[_ROW_ZETA]))
    _use_auth()

    response = client.delete("/library/album/a1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}


def test_remove_library_item_not_found_returns_library_item_not_found():
    _use_db(_fake_delete_db(data=[]))
    _use_auth()

    response = client.delete("/library/album/a1")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "library_item_not_found"}


def test_remove_library_item_invalid_kind_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.delete("/library/song/a1")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_remove_library_item_upstream_failure_returns_upstream_error():
    _use_db(_fake_delete_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.delete("/library/album/a1")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_remove_library_item_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_delete_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.delete("/library/album/a1")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_remove_library_item_scopes_delete_to_authenticated_user():
    db = _fake_delete_db(data=[_ROW_ZETA])
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.delete("/library/album/a1")

    assert response.status_code == 200
    db.table.return_value.delete.return_value.eq.assert_called_once_with(
        "user_id", "other-user-id"
    )


def test_unauthenticated_list_request_returns_unauthorized():
    response = client.get("/library")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
