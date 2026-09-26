# test/routes/test_library.py
#
# Tests for the library endpoints.
#
# Tested:
# - GET /library returns the user's unified library (own playlists and
#   saved items), cursor-paginated: data.items + data.page
# - The first page (no cursor) always starts with a fixed "liked songs"
#   entry, the same id/title as GET /playlists/liked; it never counts
#   against limit or total and never appears on a cursored page
# - A single fixed order over library_entries: added_at desc, row_id as
#   the tiebreaker, mixing own playlists and saved items
# - has_more/next_cursor derive from the limit+1 probe row, and total is
#   present (exact) only on the first page, null on a cursored one --
#   neither counts the fixed entry
# - Returns 200 ok:true with items == [liked entry] on an empty first
#   page -- not items: [] and not "no_library_items"
# - sort and order query params are ignored: an unknown value no longer
#   answers 422, and a valid-looking one does not change the order
# - An empty cursor (?cursor=) is the absent cursor: first page, with the
#   liked entry
# - A cursor with the pre-discriminator shape ({"k", "i"}) is 422
#   invalid_cursor, not a 500
# - A cursor emitted under a sort key this endpoint no longer has
#   (title) is 422 invalid_cursor
# - A garbage cursor is 422 invalid_cursor without reaching the database
# - Returns 422 invalid_request when limit is outside 1..100, without
#   reaching the database
# - Every list query is scoped to the authenticated user's id
# - Returns 502/504 when the list query fails or times out
# - Each view row maps to its LibraryEntry shape by kind/source: own
#   playlist (source "user", no subtitle), saved album (subtitle =
#   artist), saved playlist (subtitle = creator); row_id, user_id and
#   added_at never leak into a response item
# - A row that cannot build a LibraryEntry (e.g. a null title) is 502
#   upstream_error, never a null field in the response
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
# - Happy path with the fixed entry, pagination continuation and end of
#   collection, expected empty state, invalid input, upstream failure,
#   upstream timeout, user scoping, unauthenticated access, column
#   mapping by source
#
# Database access is overridden through get_user_db (core/auth.py).
#
# Run with: pytest test/routes/test_library.py -v
#
# SEE: routes/library.py, services/library_service.py,
# services/playlist_service.py, core/pagination.py,
# db/migrations/035_library_entries_view.sql

import base64
import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.auth import get_current_user_id, get_user_db
from core.pagination import (
    SortKey,
    ValueType,
    decode_cursor,
    encode_cursor,
    keyset_filter,
)

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"

# Mirrors the sort key declared in services/library_service.py, used only
# to build and decode cursors for these tests -- never imported from the
# service, so the tests fail if the two drift apart.
_ENTRIES_SORT = SortKey(
    "added_at", ValueType.TIMESTAMP, id_column="row_id", id_type=ValueType.UUID
)

_ROW_ID_OWN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ROW_ID_ALBUM = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_ROW_ID_PLAYLIST = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_ID_ZETA = "cccccccc-cccc-cccc-cccc-cccccccccccc"

# A row from the playlists branch of the view: no subtitle ever, thumbnail
# derived by the view itself (not something this service computes).
_VIEW_OWN_PLAYLIST = {
    "row_id": _ROW_ID_OWN,
    "kind": "playlist",
    "source": "user",
    "id": _ROW_ID_OWN,
    "title": "My Mix",
    "thumbnail_url": None,
    "subtitle": None,
    "added_at": "2026-01-03T00:00:00Z",
}

# A row from the library_items branch: a saved album, subtitle is the
# artist.
_VIEW_SAVED_ALBUM = {
    "row_id": _ROW_ID_ALBUM,
    "kind": "album",
    "source": "spotify",
    "id": "album-1",
    "title": "Zeta Album",
    "thumbnail_url": "https://example.com/zeta.png",
    "subtitle": "Some Artist",
    "added_at": "2026-01-02T00:00:00Z",
}

# A row from the library_items branch: a saved playlist, subtitle is the
# creator.
_VIEW_SAVED_PLAYLIST = {
    "row_id": _ROW_ID_PLAYLIST,
    "kind": "playlist",
    "source": "external",
    "id": "playlist-ext-1",
    "title": "Friends Mix",
    "thumbnail_url": "https://example.com/friends.png",
    "subtitle": "Some Creator",
    "added_at": "2026-01-01T00:00:00Z",
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

_LIKED_ENTRY = {
    "kind": "playlist",
    "id": "liked",
    "title": "liked",
    "thumbnail_url": None,
    "subtitle": None,
    "source": "liked",
}


def _entry(row: dict) -> dict:
    # A view row as it looks once it crosses into data.items: row_id and
    # added_at never leave the service, they only exist to drive the
    # cursor.
    return {
        key: value for key, value in row.items() if key not in ("row_id", "added_at")
    }


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_user_db, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _use_db(db):
    app.dependency_overrides[get_user_db] = lambda: db


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


def test_first_page_starts_with_the_liked_entry_then_view_rows():
    db = _fake_list_db(
        data=[_VIEW_OWN_PLAYLIST, _VIEW_SAVED_ALBUM, _VIEW_SAVED_PLAYLIST], count=3
    )
    _use_db(db)
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "items": [
            _LIKED_ENTRY,
            _entry(_VIEW_OWN_PLAYLIST),
            _entry(_VIEW_SAVED_ALBUM),
            _entry(_VIEW_SAVED_PLAYLIST),
        ],
        "page": {
            "limit": 50,
            "next_cursor": None,
            "has_more": False,
            "total": 3,
        },
    }
    db.table.assert_called_once_with("library_entries")
    assert db.table.return_value.select.call_args.kwargs["count"] == "exact"
    query = _chain(db, "table", "select", "eq")
    query.order.assert_called_once_with("added_at", desc=True)
    query.order.return_value.order.assert_called_once_with("row_id", desc=True)
    query.order.return_value.order.return_value.limit.assert_called_once_with(51)


def test_empty_library_returns_only_the_liked_entry():
    _use_db(_fake_list_db(data=[], count=0))
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [_LIKED_ENTRY],
            "page": {
                "limit": 50,
                "next_cursor": None,
                "has_more": False,
                "total": 0,
            },
        },
    }


def test_liked_entry_does_not_count_against_limit_or_total():
    db = _fake_list_db(data=[_VIEW_OWN_PLAYLIST, _VIEW_SAVED_ALBUM], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_LIKED_ENTRY, _entry(_VIEW_OWN_PLAYLIST)]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert body["page"]["total"] == 2
    query = _chain(db, "table", "select", "eq")
    query.order.return_value.order.return_value.limit.assert_called_once_with(2)


def test_cursored_page_has_no_liked_entry_and_continues_the_walk():
    first_db = _fake_list_db(data=[_VIEW_OWN_PLAYLIST, _VIEW_SAVED_ALBUM], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get("/library", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_db(data=[_VIEW_SAVED_PLAYLIST], count=None, cursor=True)
    _use_db(second_db)

    response = client.get("/library", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_entry(_VIEW_SAVED_PLAYLIST)]
    assert body["page"]["total"] is None
    assert body["page"]["has_more"] is False
    assert body["page"]["next_cursor"] is None

    base = _chain(second_db, "table", "select", "eq")
    expected_cursor = decode_cursor(next_cursor, _ENTRIES_SORT)
    base.or_.assert_called_once_with(keyset_filter(_ENTRIES_SORT, expected_cursor))
    assert second_db.table.return_value.select.call_args.kwargs["count"] is None


def test_cursored_page_with_more_rows_keeps_has_more():
    first_db = _fake_list_db(data=[_VIEW_OWN_PLAYLIST, _VIEW_SAVED_ALBUM], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get("/library", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_db(
        data=[_VIEW_SAVED_ALBUM, _VIEW_SAVED_PLAYLIST], count=None, cursor=True
    )
    _use_db(second_db)

    response = client.get("/library", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_entry(_VIEW_SAVED_ALBUM)]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert body["page"]["total"] is None


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_VIEW_OWN_PLAYLIST, _VIEW_SAVED_ALBUM),
        (
            {**_VIEW_SAVED_ALBUM, "added_at": "2026-01-04T00:00:00Z"},
            _VIEW_OWN_PLAYLIST,
        ),
    ],
    ids=["own-playlist-newer", "saved-album-newer"],
)
def test_entries_keep_the_view_order_across_sources(first, second):
    db = _fake_list_db(data=[first, second], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_LIKED_ENTRY, _entry(first), _entry(second)]
    db.table.assert_called_once_with("library_entries")
    query = _chain(db, "table", "select", "eq")
    query.order.assert_called_once_with("added_at", desc=True)
    query.order.return_value.order.assert_called_once_with("row_id", desc=True)


def test_view_rows_map_to_library_entries():
    db = _fake_list_db(
        data=[_VIEW_OWN_PLAYLIST, _VIEW_SAVED_ALBUM, _VIEW_SAVED_PLAYLIST], count=3
    )
    _use_db(db)
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 200
    items = response.json()["data"]["items"]

    own_playlist, saved_album, saved_playlist = items[1], items[2], items[3]
    assert own_playlist == {
        "kind": "playlist",
        "id": _ROW_ID_OWN,
        "title": "My Mix",
        "thumbnail_url": None,
        "subtitle": None,
        "source": "user",
    }
    assert saved_album == {
        "kind": "album",
        "id": "album-1",
        "title": "Zeta Album",
        "thumbnail_url": "https://example.com/zeta.png",
        "subtitle": "Some Artist",
        "source": "spotify",
    }
    assert saved_playlist == {
        "kind": "playlist",
        "id": "playlist-ext-1",
        "title": "Friends Mix",
        "thumbnail_url": "https://example.com/friends.png",
        "subtitle": "Some Creator",
        "source": "external",
    }
    for item in items:
        assert "row_id" not in item
        assert "user_id" not in item
        assert "added_at" not in item


def test_view_row_with_null_title_returns_upstream_error():
    row = {**_VIEW_OWN_PLAYLIST, "title": None}
    _use_db(_fake_list_db(data=[row], count=1))
    _use_auth()

    response = client.get("/library")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_sort_and_order_params_are_ignored():
    db = _fake_list_db(data=[_VIEW_OWN_PLAYLIST], count=1)
    _use_db(db)
    _use_auth()

    response = client.get(
        "/library", params={"sort": "title", "order": "asc", "bogus": "x"}
    )

    assert response.status_code == 200
    query = _chain(db, "table", "select", "eq")
    query.order.assert_called_once_with("added_at", desc=True)


def test_empty_cursor_is_the_absent_cursor():
    db = _fake_list_db(data=[_VIEW_OWN_PLAYLIST], count=1)
    _use_db(db)
    _use_auth()

    response = client.get("/library", params={"cursor": ""})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_LIKED_ENTRY, _entry(_VIEW_OWN_PLAYLIST)]
    assert body["page"]["total"] == 1
    assert db.table.return_value.select.call_args.kwargs["count"] == "exact"


def test_cursor_with_the_pre_discriminator_shape_is_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    raw = json.dumps({"k": "2026-01-01T00:00:00+00:00", "i": _ROW_ID_OWN}).encode()
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


def test_cursor_from_the_removed_title_sort_is_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    removed_sort = SortKey("title", ValueType.TEXT, descending=False)
    cursor = encode_cursor("some-title", _ROW_ID_OWN, removed_sort)

    response = client.get("/library", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
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
    db = _fake_list_db(data=[_VIEW_SAVED_ALBUM], count=1)
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
