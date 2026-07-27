# test/routes/test_bug_reports.py
#
# Tests for the bug reports endpoints.
#
# Tested:
# - POST /bug-reports creates a report for the authenticated user
# - reporter_id in the response always comes from the token, never the body
# - status is always "open" on create, not settable by the client
# - Returns 422 invalid_request for an invalid category, a description
#   outside 5-2000 chars, an invalid entity_type, sending only one of
#   entity_type/entity_id, or sending status in the body
# - Returns 401 unauthorized and 502/504 on upstream failure
# - GET /bug-reports/me lists only the authenticated user's own reports,
#   scoped by reporter_id
# - Returns 200 with ok:false reason no_bug_reports when empty
# - Returns 401 unauthorized and 502/504 on upstream failure
# - GET /bug-reports (admin) lists reports from every reporter, unscoped
# - Returns 403 forbidden for a non-admin caller
# - Returns 200 with ok:false reason no_bug_reports when empty
# - Returns 401 unauthorized and 502/504 on upstream failure
# - PATCH /bug-reports/{id} (admin) updates status to open or closed
# - Returns 403 forbidden for a non-admin caller
# - Returns 404 report_not_found for an unknown id
# - Returns 422 invalid_request for an invalid status value
# - Returns 401 unauthorized and 502/504 on upstream failure
#
# What is covered:
# - Happy path, expected empty state, invalid input, not found, upstream
#   failure, upstream timeout, unauthenticated access, forbidden
#   (non-admin), user scoping vs. deliberately unscoped admin listing
#
# Run with: pytest test/routes/test_bug_reports.py -v
#
# SEE: routes/bug_reports.py, services/bug_report_service.py

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.auth import get_current_profile, get_current_user_id
from core.database import get_db
from models.profiles import Profile, Role

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
_ADMIN_ID = "33333333-3333-3333-3333-333333333333"
_REPORT_ID = "44444444-4444-4444-4444-444444444444"

_CREATE_BODY = {
    "category": "crash",
    "description": "App crashes when opening a playlist.",
}

_REPORT_ROW = {
    "id": _REPORT_ID,
    "reporter_id": _USER_ID,
    "category": "crash",
    "description": "App crashes when opening a playlist.",
    "entity_type": None,
    "entity_id": None,
    "status": "open",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}

_OTHER_REPORT_ROW = {
    **_REPORT_ROW,
    "id": "55555555-5555-5555-5555-555555555555",
    "reporter_id": _OTHER_USER_ID,
}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user_id, None)
    app.dependency_overrides.pop(get_current_profile, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _use_admin(admin_id=_ADMIN_ID):
    app.dependency_overrides[get_current_profile] = lambda: Profile(
        id=admin_id,
        role=Role.ADMIN,
        username="admin",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def _use_non_admin(user_id=_USER_ID):
    app.dependency_overrides[get_current_profile] = lambda: Profile(
        id=user_id,
        role=Role.USER,
        username="alice",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def _use_db(db):
    app.dependency_overrides[get_db] = lambda: db


def _fake_create_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.insert.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_list_mine_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.select.return_value.eq.return_value.order.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_list_all_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.select.return_value.order.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_update_db(data=None, error=None):
    db = MagicMock()
    query = (
        db.table.return_value.update.return_value.eq.return_value.select.return_value
    )
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


# --- POST /bug-reports ---


def test_create_bug_report_success():
    db = _fake_create_db(data=[_REPORT_ROW])
    _use_db(db)
    _use_auth()

    response = client.post("/bug-reports", json=_CREATE_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == _REPORT_ROW


def test_create_bug_report_rejects_reporter_id_in_body():
    db = _fake_create_db(data=[_REPORT_ROW])
    _use_db(db)
    _use_auth()
    body = {**_CREATE_BODY, "reporter_id": "attacker-id"}

    response = client.post("/bug-reports", json=body)

    assert response.status_code == 422


def test_create_bug_report_always_defaults_status_open():
    db = _fake_create_db(data=[_REPORT_ROW])
    _use_db(db)
    _use_auth()

    client.post("/bug-reports", json=_CREATE_BODY)

    called_payload = db.table.return_value.insert.call_args[0][0]
    assert called_payload["status"] == "open"
    assert called_payload["reporter_id"] == _USER_ID


def test_create_bug_report_invalid_category_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_CREATE_BODY, "category": "bogus"}

    response = client.post("/bug-reports", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_bug_report_description_too_short_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_CREATE_BODY, "description": "hi"}

    response = client.post("/bug-reports", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_bug_report_description_too_long_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_CREATE_BODY, "description": "x" * 2001}

    response = client.post("/bug-reports", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_bug_report_entity_type_without_entity_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_CREATE_BODY, "entity_type": "track"}

    response = client.post("/bug-reports", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_bug_report_entity_id_without_entity_type_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_CREATE_BODY, "entity_id": "track-1"}

    response = client.post("/bug-reports", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_bug_report_invalid_entity_type_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_CREATE_BODY, "entity_type": "genre", "entity_id": "g1"}

    response = client.post("/bug-reports", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_bug_report_rejects_client_set_status():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    body = {**_CREATE_BODY, "status": "closed"}

    response = client.post("/bug-reports", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_bug_report_unauthenticated_returns_unauthorized():
    response = client.post("/bug-reports", json=_CREATE_BODY)

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_create_bug_report_upstream_failure_returns_upstream_error():
    _use_db(_fake_create_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post("/bug-reports", json=_CREATE_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_create_bug_report_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_create_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post("/bug-reports", json=_CREATE_BODY)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


# --- GET /bug-reports/me ---


def test_list_my_bug_reports_returns_own_reports():
    db = _fake_list_mine_db(data=[_REPORT_ROW])
    _use_db(db)
    _use_auth()

    response = client.get("/bug-reports/me")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"bug_reports": [_REPORT_ROW]}
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "reporter_id", _USER_ID
    )


def test_list_my_bug_reports_empty_returns_no_bug_reports():
    _use_db(_fake_list_mine_db(data=[]))
    _use_auth()

    response = client.get("/bug-reports/me")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_bug_reports"}


def test_list_my_bug_reports_unauthenticated_returns_unauthorized():
    response = client.get("/bug-reports/me")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_list_my_bug_reports_upstream_failure_returns_upstream_error():
    _use_db(_fake_list_mine_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/bug-reports/me")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_list_my_bug_reports_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_list_mine_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/bug-reports/me")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


# --- GET /bug-reports (admin) ---


def test_list_all_bug_reports_returns_reports_from_every_reporter():
    db = _fake_list_all_db(data=[_REPORT_ROW, _OTHER_REPORT_ROW])
    _use_db(db)
    _use_admin()

    response = client.get("/bug-reports")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"bug_reports": [_REPORT_ROW, _OTHER_REPORT_ROW]}
    db.table.return_value.select.return_value.eq.assert_not_called()


def test_list_all_bug_reports_forbidden_for_non_admin():
    db = MagicMock()
    _use_db(db)
    _use_non_admin()

    response = client.get("/bug-reports")

    assert response.status_code == 403
    assert response.json() == {"ok": False, "reason": "forbidden"}
    db.table.assert_not_called()


def test_list_all_bug_reports_empty_returns_no_bug_reports():
    _use_db(_fake_list_all_db(data=[]))
    _use_admin()

    response = client.get("/bug-reports")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_bug_reports"}


def test_list_all_bug_reports_unauthenticated_returns_unauthorized():
    response = client.get("/bug-reports")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_list_all_bug_reports_upstream_failure_returns_upstream_error():
    _use_db(_fake_list_all_db(error=APIError({"message": "connection refused"})))
    _use_admin()

    response = client.get("/bug-reports")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_list_all_bug_reports_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_list_all_db(error=httpx.ReadTimeout("timed out")))
    _use_admin()

    response = client.get("/bug-reports")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


# --- PATCH /bug-reports/{id} (admin) ---


def test_update_bug_report_status_success():
    updated_row = {**_REPORT_ROW, "status": "closed"}
    db = _fake_update_db(data=[updated_row])
    _use_db(db)
    _use_admin()

    response = client.patch(f"/bug-reports/{_REPORT_ID}", json={"status": "closed"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == updated_row
    db.table.return_value.update.assert_called_once_with({"status": "closed"})
    db.table.return_value.update.return_value.eq.assert_called_once_with(
        "id", _REPORT_ID
    )


def test_update_bug_report_status_forbidden_for_non_admin():
    db = MagicMock()
    _use_db(db)
    _use_non_admin()

    response = client.patch(f"/bug-reports/{_REPORT_ID}", json={"status": "closed"})

    assert response.status_code == 403
    assert response.json() == {"ok": False, "reason": "forbidden"}
    db.table.assert_not_called()


def test_update_bug_report_status_unknown_id_returns_report_not_found():
    _use_db(_fake_update_db(data=[]))
    _use_admin()

    response = client.patch(f"/bug-reports/{_REPORT_ID}", json={"status": "closed"})

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "report_not_found"}


def test_update_bug_report_status_invalid_value_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_admin()

    response = client.patch(f"/bug-reports/{_REPORT_ID}", json={"status": "bogus"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_update_bug_report_status_unauthenticated_returns_unauthorized():
    response = client.patch(f"/bug-reports/{_REPORT_ID}", json={"status": "closed"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_update_bug_report_status_upstream_failure_returns_upstream_error():
    _use_db(_fake_update_db(error=APIError({"message": "connection refused"})))
    _use_admin()

    response = client.patch(f"/bug-reports/{_REPORT_ID}", json={"status": "closed"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_update_bug_report_status_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_update_db(error=httpx.ReadTimeout("timed out")))
    _use_admin()

    response = client.patch(f"/bug-reports/{_REPORT_ID}", json={"status": "closed"})

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
