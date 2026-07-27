# test/routes/test_profile.py
#
# Tests for the profile endpoints.
#
# Tested:
# - GET /profile/me returns the authenticated user's profile
# - Returns 404 profile_not_found when no profile row matches the user
# - Returns 401 unauthorized when there is no Authorization header
# - Returns 502/504 when the profile lookup fails or times out
# - PATCH /profile/me updates username, display_name and avatar_url and
#   returns the updated profile
# - PATCH /profile/me with a partial body only changes the sent field,
#   the update payload contains only what was sent
# - PATCH /profile/me rejects an unknown field (e.g. role) with 422
#   invalid_request, without reaching the database
# - PATCH /profile/me rejects a malformed username (bad chars, too
#   short, too long) with 422 invalid_request, without reaching the
#   database
# - PATCH /profile/me rejects an explicit null username with 422
#   invalid_request, without reaching the database
# - PATCH /profile/me returns 409 username_taken when the update hits
#   the unique constraint
# - PATCH /profile/me returns 404 profile_not_found when the profile
#   doesn't exist, without attempting the update
# - Returns 401 unauthorized when there is no Authorization header
# - Returns 502/504 when the update fails or times out
# - Every query is scoped to the authenticated user's id
#
# What is covered:
# - Happy path, partial update, invalid input, conflict, parent not
#   found, upstream failure, upstream timeout, unauthenticated access,
#   user scoping
#
# Run with: pytest test/routes/test_profile.py -v
#
# SEE: routes/profile.py, services/profile_service.py, models/profiles.py

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

_PROFILE_ROW = {
    "id": _USER_ID,
    "role": "user",
    "username": "alice",
    "display_name": "Alice A",
    "avatar_url": "https://example.com/alice.png",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
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


def _fake_db(get_data=None, get_error=None, update_data=None, update_error=None):
    db = MagicMock()
    table = db.table.return_value

    get_query = table.select.return_value.eq.return_value
    if get_error is not None:
        get_query.execute.side_effect = get_error
    else:
        get_query.execute.return_value = MagicMock(data=get_data)

    update_query = table.update.return_value.eq.return_value.select.return_value
    if update_error is not None:
        update_query.execute.side_effect = update_error
    else:
        update_query.execute.return_value = MagicMock(data=update_data)

    return db


def test_get_my_profile_returns_authenticated_users_profile():
    _use_db(_fake_db(get_data=[_PROFILE_ROW]))
    _use_auth()

    response = client.get("/profile/me")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == _PROFILE_ROW


def test_get_my_profile_missing_returns_profile_not_found():
    _use_db(_fake_db(get_data=[]))
    _use_auth()

    response = client.get("/profile/me")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "profile_not_found"}


def test_get_my_profile_unauthenticated_returns_unauthorized():
    response = client.get("/profile/me")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_get_my_profile_lookup_failure_returns_upstream_error():
    _use_db(_fake_db(get_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/profile/me")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_my_profile_lookup_timeout_returns_upstream_timeout():
    _use_db(_fake_db(get_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/profile/me")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_my_profile_scopes_query_to_authenticated_user():
    db = _fake_db(get_data=[_PROFILE_ROW])
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.get("/profile/me")

    assert response.status_code == 200
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "id", "other-user-id"
    )


def test_update_my_profile_updates_editable_fields():
    updated_row = {
        **_PROFILE_ROW,
        "username": "alice2",
        "display_name": "Alice Two",
        "avatar_url": "https://example.com/alice2.png",
    }
    db = _fake_db(get_data=[_PROFILE_ROW], update_data=[updated_row])
    _use_db(db)
    _use_auth()
    body = {
        "username": "alice2",
        "display_name": "Alice Two",
        "avatar_url": "https://example.com/alice2.png",
    }

    response = client.patch("/profile/me", json=body)

    assert response.status_code == 200
    response_body = response.json()
    assert response_body["ok"] is True
    assert response_body["data"] == updated_row
    db.table.return_value.update.assert_called_once_with(body)


def test_update_my_profile_partial_body_only_sends_provided_field():
    updated_row = {**_PROFILE_ROW, "avatar_url": "https://example.com/new.png"}
    db = _fake_db(get_data=[_PROFILE_ROW], update_data=[updated_row])
    _use_db(db)
    _use_auth()

    response = client.patch(
        "/profile/me", json={"avatar_url": "https://example.com/new.png"}
    )

    assert response.status_code == 200
    db.table.return_value.update.assert_called_once_with(
        {"avatar_url": "https://example.com/new.png"}
    )


def test_update_my_profile_rejects_unknown_field():
    db = _fake_db(get_data=[_PROFILE_ROW])
    _use_db(db)
    _use_auth()

    response = client.patch("/profile/me", json={"role": "admin"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.return_value.update.assert_not_called()


@pytest.mark.parametrize(
    "username",
    ["ab", "a" * 31, "bad-name!", "has space"],
)
def test_update_my_profile_rejects_malformed_username(username):
    db = _fake_db(get_data=[_PROFILE_ROW])
    _use_db(db)
    _use_auth()

    response = client.patch("/profile/me", json={"username": username})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.return_value.update.assert_not_called()


def test_update_my_profile_rejects_explicit_null_username():
    db = _fake_db(get_data=[_PROFILE_ROW])
    _use_db(db)
    _use_auth()

    response = client.patch("/profile/me", json={"username": None})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.return_value.update.assert_not_called()


def test_update_my_profile_taken_username_returns_conflict():
    db = _fake_db(
        get_data=[_PROFILE_ROW],
        update_error=APIError(
            {
                "message": "duplicate key value violates unique constraint",
                "code": "23505",
            }
        ),
    )
    _use_db(db)
    _use_auth()

    response = client.patch("/profile/me", json={"username": "taken"})

    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "username_taken"}


def test_update_my_profile_missing_returns_profile_not_found():
    db = _fake_db(get_data=[])
    _use_db(db)
    _use_auth()

    response = client.patch("/profile/me", json={"display_name": "New Name"})

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "profile_not_found"}
    db.table.return_value.update.assert_not_called()


def test_update_my_profile_unauthenticated_returns_unauthorized():
    response = client.patch("/profile/me", json={"display_name": "New Name"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_update_my_profile_upstream_failure_returns_upstream_error():
    db = _fake_db(
        get_data=[_PROFILE_ROW],
        update_error=APIError({"message": "connection refused"}),
    )
    _use_db(db)
    _use_auth()

    response = client.patch("/profile/me", json={"display_name": "New Name"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_update_my_profile_upstream_timeout_returns_upstream_timeout():
    db = _fake_db(get_data=[_PROFILE_ROW], update_error=httpx.ReadTimeout("timed out"))
    _use_db(db)
    _use_auth()

    response = client.patch("/profile/me", json={"display_name": "New Name"})

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_update_my_profile_scopes_update_to_authenticated_user():
    updated_row = {**_PROFILE_ROW, "id": "other-user-id"}
    db = _fake_db(
        get_data=[{**_PROFILE_ROW, "id": "other-user-id"}],
        update_data=[updated_row],
    )
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.patch("/profile/me", json={"display_name": "New Name"})

    assert response.status_code == 200
    db.table.return_value.update.return_value.eq.assert_called_once_with(
        "id", "other-user-id"
    )
