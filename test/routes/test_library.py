# test/routes/test_library.py
#
# Tests for the library endpoints.
#
# Tested:
# - GET /library returns the user's items, cursor-paginated: data.items +
#   data.page
# - Each of the four sort/order combinations (added_at/title x asc/desc)
#   orders by its column, then by id, in the same direction
# - A next_cursor emitted under one combination round-trips and continues
#   the page under that same combination, for all four
# - A cursor emitted under one combination is 422 invalid_cursor when
#   reused under another — mismatched column, mismatched direction, or
#   both — without reaching the database
# - A cursor with the pre-discriminator shape ({"k", "i"}) is 422
#   invalid_cursor, not a 500
# - has_more/next_cursor derive from the limit+1 probe row, and total is
#   present (exact) only on the first page, null on a cursored one
# - Returns 200 ok:true with an empty first page when the library is
#   empty — not "no_library_items"
# - Returns 422 invalid_request when sort or order is not a valid value,
#   or a limit outside 1..100, without reaching the database
# - A garbage cursor is 422 invalid_cursor without reaching the database
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
# - Happy path, pagination continuation and end of collection, the four
#   sort/order combinations and their cursor round-trip, cross-combination
#   cursor rejection, expected empty state, invalid input, not found,
#   upstream failure, upstream timeout, user scoping, unauthenticated
#   access
#
# Run with: pytest test/routes/test_library.py -v
#
# SEE: routes/library.py, services/library_service.py, core/pagination.py

import base64
import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.auth import get_current_user_id
from core.database import get_db
from core.pagination import SortKey, ValueType, decode_cursor, keyset_filter

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"

# Mirrors the sort keys declared in services/library_service.py, used only
# to build and decode cursors for these tests — never imported from the
# service, so the tests fail if the two drift apart.
_ADDED_DESC = SortKey("added_at", ValueType.TIMESTAMP)
_ADDED_ASC = SortKey("added_at", ValueType.TIMESTAMP, descending=False)
_TITLE_DESC = SortKey("title", ValueType.TEXT)
_TITLE_ASC = SortKey("title", ValueType.TEXT, descending=False)

_ID_ALPHA = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ID_ZETA = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_ROW_ALPHA = {
    "id": _ID_ALPHA,
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
    "id": _ID_ZETA,
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


def _item(row: dict) -> dict:
    # The endpoint never exposes the internal row id: this is what a row
    # looks like once it crosses into data.items.
    return {key: value for key, value in row.items() if key != "id"}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _use_db(db):
    app.dependency_overrides[get_db] = lambda: db


def _chain(mock, *names):
    node = mock
    for name in names:
        node = getattr(node, name).return_value
    return node


def _fake_list_db(data=None, count=None, error=None, cursor=False):
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


# --- GET /library ---------------------------------------------------------


def test_returns_library_items_with_data_and_exact_total():
    db = _fake_list_db(data=[_ROW_ZETA, _ROW_ALPHA], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "items": [_item(_ROW_ZETA), _item(_ROW_ALPHA)],
        "page": {
            "limit": 50,
            "next_cursor": None,
            "has_more": False,
            "total": 2,
        },
    }
    assert db.table.return_value.select.call_args.kwargs["count"] == "exact"
    query = _chain(db, "table", "select", "eq")
    query.order.assert_called_once_with("added_at", desc=True)
    query.order.return_value.order.assert_called_once_with("id", desc=True)
    query.order.return_value.order.return_value.limit.assert_called_once_with(51)


def test_first_page_with_limit_has_more_and_next_cursor():
    db = _fake_list_db(data=[_ROW_ZETA, _ROW_ALPHA], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_item(_ROW_ZETA)]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert body["page"]["total"] == 2
    query = _chain(db, "table", "select", "eq")
    query.order.return_value.order.return_value.limit.assert_called_once_with(2)


def test_next_page_via_cursor_returns_remaining_items_without_repeats():
    first_db = _fake_list_db(data=[_ROW_ZETA, _ROW_ALPHA], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get("/library", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_db(data=[_ROW_ALPHA], count=None, cursor=True)
    _use_db(second_db)

    response = client.get("/library", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_item(_ROW_ALPHA)]
    assert body["page"]["total"] is None
    assert body["page"]["has_more"] is False
    assert body["page"]["next_cursor"] is None

    base = _chain(second_db, "table", "select", "eq")
    expected_cursor = decode_cursor(next_cursor, _ADDED_DESC)
    base.or_.assert_called_once_with(keyset_filter(_ADDED_DESC, expected_cursor))
    select_call = second_db.table.return_value.select.call_args
    assert select_call.kwargs["count"] is None


def test_empty_library_is_an_empty_first_page():
    _use_db(_fake_list_db(data=[], count=0))
    _use_auth()

    response = client.get("/library")

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


@pytest.mark.parametrize(
    ("sort", "order", "column", "desc"),
    [
        ("added_at", "desc", "added_at", True),
        ("added_at", "asc", "added_at", False),
        ("title", "desc", "title", True),
        ("title", "asc", "title", False),
    ],
)
def test_each_combination_orders_by_its_column_then_id(sort, order, column, desc):
    db = _fake_list_db(data=[_ROW_ZETA, _ROW_ALPHA], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"sort": sort, "order": order})

    assert response.status_code == 200
    query = _chain(db, "table", "select", "eq")
    query.order.assert_called_once_with(column, desc=desc)
    query.order.return_value.order.assert_called_once_with("id", desc=desc)


@pytest.mark.parametrize(
    ("sort", "order", "sort_key"),
    [
        ("added_at", "desc", _ADDED_DESC),
        ("added_at", "asc", _ADDED_ASC),
        ("title", "desc", _TITLE_DESC),
        ("title", "asc", _TITLE_ASC),
    ],
)
def test_cursor_round_trips_within_the_same_combination(sort, order, sort_key):
    first_db = _fake_list_db(data=[_ROW_ZETA, _ROW_ALPHA], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get(
        "/library", params={"sort": sort, "order": order, "limit": 1}
    )
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]
    assert next_cursor is not None

    second_db = _fake_list_db(data=[_ROW_ALPHA], count=None, cursor=True)
    _use_db(second_db)

    response = client.get(
        "/library",
        params={"sort": sort, "order": order, "limit": 1, "cursor": next_cursor},
    )

    assert response.status_code == 200
    base = _chain(second_db, "table", "select", "eq")
    expected_cursor = decode_cursor(next_cursor, sort_key)
    base.or_.assert_called_once_with(keyset_filter(sort_key, expected_cursor))


@pytest.mark.parametrize(
    ("emitted", "reused"),
    [
        (("added_at", "desc"), ("title", "asc")),
        (("added_at", "desc"), ("added_at", "asc")),
        (("title", "asc"), ("title", "desc")),
        (("added_at", "desc"), ("title", "desc")),
    ],
    ids=[
        "different-column-and-direction",
        "same-column-different-direction",
        "same-column-different-direction-title",
        "different-column-same-direction",
    ],
)
def test_cursor_from_one_combination_is_rejected_by_another(emitted, reused):
    emitted_sort, emitted_order = emitted
    reused_sort, reused_order = reused

    first_db = _fake_list_db(data=[_ROW_ZETA, _ROW_ALPHA], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get(
        "/library",
        params={"sort": emitted_sort, "order": emitted_order, "limit": 1},
    )
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    db = MagicMock()
    _use_db(db)

    response = client.get(
        "/library",
        params={"sort": reused_sort, "order": reused_order, "cursor": next_cursor},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


def test_cursor_with_the_pre_discriminator_shape_is_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    raw = json.dumps({"k": "2026-01-01T00:00:00+00:00", "i": _ID_ALPHA}).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    response = client.get("/library", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


def test_garbage_cursor_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"cursor": "???"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


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


@pytest.mark.parametrize("limit", [0, -5, 101, "abc"])
def test_invalid_limit_returns_invalid_request(limit):
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"limit": limit})

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
    db = _fake_list_db(data=[_ROW_ZETA], count=1)
    _use_db(db)
    _use_auth(user_id="other-user-id")

    response = client.get("/library")

    assert response.status_code == 200
    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "user_id", "other-user-id"
    )


def test_unauthenticated_list_request_returns_unauthorized():
    response = client.get("/library")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- POST /library ---------------------------------------------------------


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


# --- DELETE /library/{kind}/{external_id} -----------------------------------


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
