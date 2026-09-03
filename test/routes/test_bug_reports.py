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
# - Returns 502 when the insert returns no row
# - GET /bug-reports/me lists only the authenticated user's own reports,
#   scoped by reporter_id, cursor-paginated: data.items + data.page,
#   created_at descending with id breaking ties
# - has_more/next_cursor derive from the limit+1 probe row, and total is
#   present (exact) only on the first page, null on a cursored one
# - A next_cursor round-trips: the following page continues where the
#   previous one ended, without repeating a report, and stays scoped to
#   reporter_id on every page, not only the first
# - Returns 200 ok:true with an empty first page when the caller has no
#   reports — not "no_bug_reports"
# - A garbage cursor, and one emitted by another paginated endpoint, are
#   both 422 invalid_cursor without reaching the database
# - Returns 422 invalid_request for a limit outside 1..100, without
#   reaching the database
# - reporter_id always comes from the token, never the query string, on
#   the first page and on a cursored one
# - A cursor emitted by GET /bug-reports (admin) is accepted by
#   GET /bug-reports/me and stays scoped to the caller's reporter_id
# - Returns 401 unauthorized and 502/504 on upstream failure
# - GET /bug-reports (admin) lists reports from every reporter, unscoped,
#   cursor-paginated, on both the first page and a cursored one
# - Returns 403 forbidden for a non-admin caller, with limit/cursor
#   present, without reaching the database
# - Returns 200 ok:true with an empty first page when there are no
#   reports — not "no_bug_reports"
# - Returns 401 unauthorized and 502/504 on upstream failure
# - PATCH /bug-reports/{id} (admin) updates status to open or closed
# - Returns 403 forbidden for a non-admin caller
# - Returns 404 report_not_found for an unknown id
# - Returns 422 invalid_request for an invalid status value
# - Returns 401 unauthorized and 502/504 on upstream failure
#
# What is covered:
# - Happy path, expected empty state, cursor pagination, invalid input,
#   not found, upstream failure, upstream timeout, unauthenticated
#   access, forbidden (non-admin), user scoping vs. deliberately
#   unscoped admin listing
#
# Run with: pytest test/routes/test_bug_reports.py -v
#
# SEE: routes/bug_reports.py, services/bug_report_service.py

from unittest.mock import MagicMock, call

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.auth import get_current_profile, get_current_user_id
from core.database import get_db
from core.pagination import (
    SortKey,
    ValueType,
    decode_cursor,
    encode_cursor,
    keyset_filter,
)
from models.profiles import Profile, Role

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
_ADMIN_ID = "33333333-3333-3333-3333-333333333333"
_REPORT_ID = "44444444-4444-4444-4444-444444444444"
_OLDER_REPORT_ID = "66666666-6666-6666-6666-666666666666"
_TIE_REPORT_ID = "00000000-0000-0000-0000-000000000000"

# Mirrors the sort key declared in services/bug_report_service.py, used
# only to build and decode cursors for these tests — never imported from
# the service, so the tests fail if the two drift apart. _FOREIGN_SORT is
# GET /library's, declared here for the same reason: it is what another
# paginated endpoint's cursor is tagged with.
_LIST_SORT = SortKey("created_at", ValueType.TIMESTAMP)
_FOREIGN_SORT = SortKey("added_at", ValueType.TIMESTAMP)

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

# Another reporter's report, older than _REPORT_ROW: the admin listing is
# the only one that returns it, and the older created_at keeps it on the
# far side of a cursor built from _REPORT_ROW.
_OTHER_REPORT_ROW = {
    **_REPORT_ROW,
    "id": "55555555-5555-5555-5555-555555555555",
    "reporter_id": _OTHER_USER_ID,
    "created_at": "2025-11-01T00:00:00Z",
    "updated_at": "2025-11-01T00:00:00Z",
}

# A second page's worth: older than _REPORT_ROW, same reporter, different
# id, so created_at descending is observable and the cursor built from
# either row is distinguishable.
_OLDER_REPORT_ROW = {
    **_REPORT_ROW,
    "id": _OLDER_REPORT_ID,
    "created_at": "2025-12-01T00:00:00Z",
    "updated_at": "2025-12-01T00:00:00Z",
}

# Same reporter_id and the same created_at as _REPORT_ROW, only the id
# differs — the row the id tiebreaker exists for.
_TIE_REPORT_ROW = {**_REPORT_ROW, "id": _TIE_REPORT_ID}


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


def _chain(mock, *names):
    node = mock
    for name in names:
        node = getattr(node, name).return_value
    return node


def _fake_list_mine_db(data=None, count=None, error=None, cursor=False):
    db = MagicMock()
    base = _chain(db, "table", "select", "eq")
    if cursor:
        leaf = _chain(base, "or_", "order", "order", "limit")
    else:
        leaf = _chain(base, "order", "order", "limit")

    if error is not None:
        leaf.execute.side_effect = error
    else:
        leaf.execute.return_value = MagicMock(data=data, count=count)
    return db


def _fake_list_all_db(data=None, count=None, error=None, cursor=False):
    db = MagicMock()
    base = _chain(db, "table", "select")
    if cursor:
        leaf = _chain(base, "or_", "order", "order", "limit")
    else:
        leaf = _chain(base, "order", "order", "limit")

    if error is not None:
        leaf.execute.side_effect = error
    else:
        leaf.execute.return_value = MagicMock(data=data, count=count)
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


def test_create_bug_report_without_returned_row_returns_upstream_error():
    _use_db(_fake_create_db(data=[]))
    _use_auth()

    response = client.post("/bug-reports", json=_CREATE_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


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


def test_list_my_bug_reports_returns_own_reports_newest_first_with_exact_total():
    db = _fake_list_mine_db(data=[_REPORT_ROW, _OLDER_REPORT_ROW], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/bug-reports/me")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "items": [_REPORT_ROW, _OLDER_REPORT_ROW],
        "page": {
            "limit": 50,
            "next_cursor": None,
            "has_more": False,
            "total": 2,
        },
    }
    assert db.table.return_value.select.call_args.kwargs["count"] == "exact"
    query = _chain(db, "table", "select", "eq")
    query.order.assert_called_once_with("created_at", desc=True)
    query.order.return_value.order.assert_called_once_with("id", desc=True)
    query.order.return_value.order.return_value.limit.assert_called_once_with(51)
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "reporter_id", _USER_ID
    )


def test_list_my_bug_reports_first_page_with_limit_has_more_and_next_cursor():
    db = _fake_list_mine_db(data=[_REPORT_ROW, _OLDER_REPORT_ROW], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/bug-reports/me", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_REPORT_ROW]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert body["page"]["total"] == 2
    query = _chain(db, "table", "select", "eq")
    query.order.return_value.order.return_value.limit.assert_called_once_with(2)


def test_list_my_bug_reports_next_page_via_cursor_returns_remaining_without_repeats():
    first_db = _fake_list_mine_db(data=[_REPORT_ROW, _OLDER_REPORT_ROW], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get("/bug-reports/me", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_mine_db(data=[_OLDER_REPORT_ROW], count=None, cursor=True)
    _use_db(second_db)

    response = client.get("/bug-reports/me", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_OLDER_REPORT_ROW]
    assert body["page"]["total"] is None
    assert body["page"]["has_more"] is False
    assert body["page"]["next_cursor"] is None

    base = _chain(second_db, "table", "select", "eq")
    expected_cursor = decode_cursor(next_cursor, _LIST_SORT)
    base.or_.assert_called_once_with(keyset_filter(_LIST_SORT, expected_cursor))
    assert second_db.table.return_value.select.call_args.kwargs["count"] is None


def test_list_my_bug_reports_cursor_page_still_scopes_to_reporter_id():
    first_db = _fake_list_mine_db(data=[_REPORT_ROW, _OLDER_REPORT_ROW], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get("/bug-reports/me", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_mine_db(data=[_OLDER_REPORT_ROW], count=None, cursor=True)
    _use_db(second_db)

    response = client.get("/bug-reports/me", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    select_node = second_db.table.return_value.select.return_value
    select_node.eq.assert_called_once_with("reporter_id", _USER_ID)
    expected_filter = keyset_filter(_LIST_SORT, decode_cursor(next_cursor, _LIST_SORT))
    assert select_node.mock_calls[:2] == [
        call.eq("reporter_id", _USER_ID),
        call.eq().or_(expected_filter),
    ]


def test_list_my_bug_reports_cursor_page_ignores_a_reporter_id_query_param():
    _use_auth(user_id=_OTHER_USER_ID)
    db = _fake_list_mine_db(data=[], count=None, cursor=True)
    _use_db(db)
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", _REPORT_ID, _LIST_SORT)

    response = client.get(
        "/bug-reports/me", params={"cursor": cursor, "reporter_id": _USER_ID}
    )

    assert response.status_code == 200
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "reporter_id", _OTHER_USER_ID
    )


def test_list_my_bug_reports_accepts_a_cursor_from_the_admin_listing_and_stays_scoped():
    admin_db = _fake_list_all_db(data=[_REPORT_ROW, _OLDER_REPORT_ROW], count=2)
    _use_db(admin_db)
    _use_auth(user_id=_ADMIN_ID)
    _use_admin(admin_id=_ADMIN_ID)

    admin_response = client.get("/bug-reports", params={"limit": 1})
    admin_cursor = admin_response.json()["data"]["page"]["next_cursor"]

    db = _fake_list_mine_db(data=[], count=None, cursor=True)
    _use_db(db)

    response = client.get("/bug-reports/me", params={"cursor": admin_cursor})

    assert response.status_code == 200
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "reporter_id", _ADMIN_ID
    )


def test_list_my_bug_reports_cursor_breaks_ties_on_id():
    db = _fake_list_mine_db(data=[_REPORT_ROW, _TIE_REPORT_ROW], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/bug-reports/me", params={"limit": 1})

    next_cursor = response.json()["data"]["page"]["next_cursor"]
    assert decode_cursor(next_cursor, _LIST_SORT).id == _REPORT_ID


def test_list_my_bug_reports_empty_is_an_empty_first_page():
    _use_db(_fake_list_mine_db(data=[], count=0))
    _use_auth()

    response = client.get("/bug-reports/me")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [],
            "page": {
                "limit": 50,
                "next_cursor": None,
                "has_more": False,
                "total": 0,
            },
        },
    }


def test_list_my_bug_reports_garbage_cursor_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/bug-reports/me", params={"cursor": "???"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


def test_list_my_bug_reports_cursor_from_another_endpoint_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    # Same value types as this endpoint's cursor, timestamp plus uuid: what
    # rejects it is the sort key tag it was emitted under, not the types.
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", _REPORT_ID, _FOREIGN_SORT)

    response = client.get("/bug-reports/me", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


@pytest.mark.parametrize("limit", [0, -5, 101, "abc"])
def test_list_my_bug_reports_invalid_limit_returns_invalid_request(limit):
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/bug-reports/me", params={"limit": limit})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_list_my_bug_reports_scopes_query_to_authenticated_user():
    db = _fake_list_mine_db(data=[], count=0)
    _use_db(db)
    _use_auth(user_id=_OTHER_USER_ID)

    client.get("/bug-reports/me")

    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "reporter_id", _OTHER_USER_ID
    )


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
    db = _fake_list_all_db(data=[_REPORT_ROW, _OTHER_REPORT_ROW], count=2)
    _use_db(db)
    _use_admin()

    response = client.get("/bug-reports")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "items": [_REPORT_ROW, _OTHER_REPORT_ROW],
        "page": {
            "limit": 50,
            "next_cursor": None,
            "has_more": False,
            "total": 2,
        },
    }
    assert db.table.return_value.select.call_args.kwargs["count"] == "exact"
    query = _chain(db, "table", "select")
    query.order.assert_called_once_with("created_at", desc=True)
    query.order.return_value.order.assert_called_once_with("id", desc=True)
    query.order.return_value.order.return_value.limit.assert_called_once_with(51)
    db.table.return_value.select.return_value.eq.assert_not_called()


def test_list_all_bug_reports_first_page_with_limit_has_more_and_next_cursor():
    db = _fake_list_all_db(data=[_REPORT_ROW, _OTHER_REPORT_ROW], count=2)
    _use_db(db)
    _use_admin()

    response = client.get("/bug-reports", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_REPORT_ROW]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert body["page"]["total"] == 2
    query = _chain(db, "table", "select")
    query.order.return_value.order.return_value.limit.assert_called_once_with(2)


def test_list_all_bug_reports_next_page_via_cursor_returns_remaining_without_repeats():
    first_db = _fake_list_all_db(data=[_REPORT_ROW, _OTHER_REPORT_ROW], count=2)
    _use_db(first_db)
    _use_admin()
    first_response = client.get("/bug-reports", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_all_db(data=[_OTHER_REPORT_ROW], count=None, cursor=True)
    _use_db(second_db)

    response = client.get("/bug-reports", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_OTHER_REPORT_ROW]
    assert body["page"]["total"] is None
    assert body["page"]["has_more"] is False
    assert body["page"]["next_cursor"] is None

    base = _chain(second_db, "table", "select")
    expected_cursor = decode_cursor(next_cursor, _LIST_SORT)
    base.or_.assert_called_once_with(keyset_filter(_LIST_SORT, expected_cursor))
    assert second_db.table.return_value.select.call_args.kwargs["count"] is None


def test_list_all_bug_reports_cursor_page_stays_unscoped():
    first_db = _fake_list_all_db(data=[_REPORT_ROW, _OTHER_REPORT_ROW], count=2)
    _use_db(first_db)
    _use_admin()
    first_response = client.get("/bug-reports", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_all_db(data=[_OTHER_REPORT_ROW], count=None, cursor=True)
    _use_db(second_db)

    response = client.get("/bug-reports", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    select_node = second_db.table.return_value.select.return_value
    select_node.eq.assert_not_called()
    expected_filter = keyset_filter(_LIST_SORT, decode_cursor(next_cursor, _LIST_SORT))
    assert select_node.mock_calls[:1] == [call.or_(expected_filter)]


def test_list_all_bug_reports_empty_is_an_empty_first_page():
    _use_db(_fake_list_all_db(data=[], count=0))
    _use_admin()

    response = client.get("/bug-reports")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [],
            "page": {
                "limit": 50,
                "next_cursor": None,
                "has_more": False,
                "total": 0,
            },
        },
    }


def test_list_all_bug_reports_garbage_cursor_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_admin()

    response = client.get("/bug-reports", params={"cursor": "???"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


def test_list_all_bug_reports_cursor_from_another_endpoint_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_admin()
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", _REPORT_ID, _FOREIGN_SORT)

    response = client.get("/bug-reports", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


@pytest.mark.parametrize("limit", [0, -5, 101, "abc"])
def test_list_all_bug_reports_invalid_limit_returns_invalid_request(limit):
    db = MagicMock()
    _use_db(db)
    _use_admin()

    response = client.get("/bug-reports", params={"limit": limit})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_list_all_bug_reports_forbidden_for_non_admin():
    db = MagicMock()
    _use_db(db)
    _use_non_admin()
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", _REPORT_ID, _LIST_SORT)

    response = client.get("/bug-reports", params={"limit": 2, "cursor": cursor})

    assert response.status_code == 403
    assert response.json() == {"ok": False, "reason": "forbidden"}
    db.table.assert_not_called()


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
