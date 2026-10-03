# test/routes/test_likes.py
#
# Tests for the likes endpoints.
#
# Tested:
# - GET /likes returns the user's active likes, ordered by created_at,
#   cursor-paginated: data.items + data.page
# - GET /likes with no active likes is a normal empty first page
#   (ok:true, items: [], has_more: false, total: 0) — not "no_likes"
# - has_more/next_cursor derive from the limit+1 probe row, and a real
#   next_cursor round-trips into the or_() filter of the following page
# - total is present (exact) only on the first page, null on a cursored
#   one, and the select() count mode follows that
# - An invalid cursor is 422 invalid_cursor without reaching the database
# - A limit outside 1..100 is 422 invalid_request without reaching the
#   database
# - GET /likes and /likes/sync select the like's own columns plus the
#   catalog track spread into the row (PostgREST `...tracks(...)`), which
#   is already the flat shape of Like
# - POST /likes upserts a like, keyed on (user_id, track_id), always
#   clearing deleted_at so re-liking a soft-deleted row revives it; the
#   like carries no track fields
# - POST /likes writes the track to the catalog with the service-role
#   client (get_db), refreshing the metadata, before the like
# - POST /likes without duration_seconds keeps the catalog's duration, and
#   is 422 invalid_request, writing nothing, when the track is not there
# - POST /likes returns 502 without writing the like when the catalog
#   upsert fails or returns no row
# - A list row without its catalog track is 502 (the spread gives null
#   fields)
# - Returns 422 invalid_request when a required field is missing or
#   artists is empty, without reaching the database
# - POST /likes returns 502 when the upsert returns no row
# - A user_id in the request body is never forwarded to the database;
#   only the authenticated user's id is used
# - DELETE /likes/{track_id} soft-deletes a like by setting deleted_at
# - Unliking a track that isn't liked is still 200 ok:true (idempotent)
# - GET /likes/sync returns active and soft-deleted rows changed since
#   `since`, ordered by updated_at, same pagination shape as GET /likes
# - `since` is required only on the first page (no cursor); with a
#   cursor it is optional, and if both are sent the cursor wins and
#   `since` is ignored (no gt() call)
# - Neither `since` nor `cursor` is 422 invalid_request without reaching
#   the database
# - A sync with no changes is 200 ok:true with an empty items list
# - Every query is scoped to the authenticated user's id
# - Returns 502/504 when a query fails or times out
# - An unauthenticated request returns 401 unauthorized
# - Cache-Control: GET /likes and POST /likes send private, no-cache on
#   a 200
# - Thumbnails at 544 x 544 with smart crop, rewritten when read, on
#   GET /likes, GET /likes/sync and the response of POST /likes; the
#   upsert still stores the URL as the client sent it
#
# What is covered:
# - Happy path, expected empty page, pagination continuation and end of
#   collection, invalid cursor, invalid limit, revival on re-like,
#   idempotent unlike, invalid input, upstream failure, upstream
#   timeout, user scoping, since/cursor precedence, unauthenticated
#   access
#
# Database access is overridden through get_user_db (core/auth.py) and,
# for the catalog write of POST /likes, get_db (core/database.py).
#
# Run with: pytest test/routes/test_likes.py -v
#
# SEE: routes/likes.py, services/likes_service.py, core/pagination.py

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.auth import get_current_user_id, get_user_db
from core.database import get_db
from core.pagination import (
    SortKey,
    ValueType,
    decode_cursor,
    encode_cursor,
    keyset_filter,
)

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"

_ARTIST = {"id": "artist-1", "name": "Some Artist"}

# Mirrors the sort keys declared in services/likes_service.py, used only
# to build and decode cursors for these tests — never imported from the
# service, so the tests fail if the two drift apart.
_LIST_SORT = SortKey(
    "created_at",
    ValueType.TIMESTAMP,
    descending=False,
    id_column="track_id",
    id_type=ValueType.TEXT,
)
_SYNC_SORT = SortKey(
    "updated_at",
    ValueType.TIMESTAMP,
    descending=False,
    id_column="track_id",
    id_type=ValueType.TEXT,
)

# The flat shape of Like, as the API returns it.
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

_ROW_LIKE_2 = {
    **_ROW_LIKE,
    "track_id": "t2",
    "created_at": "2026-01-02T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
}

_ROW_LIKE_DELETED = {
    **_ROW_LIKE,
    "track_id": "t3",
    "deleted_at": "2026-01-03T00:00:00Z",
    "updated_at": "2026-01-03T00:00:00Z",
}

# Mirrors _COLUMNS in services/likes_service.py, never imported from it.
_COLUMNS = (
    "track_id, created_at, updated_at, deleted_at, "
    "...tracks(title, artists, album, album_id, thumbnail_url, duration_seconds)"
)

# The six catalog fields the spread brings into the like row.
_TRACK_FIELDS = (
    "title",
    "artists",
    "album",
    "album_id",
    "thumbnail_url",
    "duration_seconds",
)


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
    app.dependency_overrides.pop(get_user_db, None)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _use_db(db):
    app.dependency_overrides[get_user_db] = lambda: db


def _use_catalog_db(db):
    app.dependency_overrides[get_db] = lambda: db


def _chain(mock, *names):
    node = mock
    for name in names:
        node = getattr(node, name).return_value
    return node


def _fake_list_db(data=None, count=None, error=None, cursor=False):
    db = MagicMock()
    base = _chain(db, "table", "select", "eq", "is_")
    if cursor:
        leaf = _chain(base, "or_", "order", "order", "limit")
    else:
        leaf = _chain(base, "order", "order", "limit")

    if error is not None:
        leaf.execute.side_effect = error
    else:
        leaf.execute.return_value = MagicMock(data=data, count=count)
    return db


def _fake_sync_db(data=None, count=None, error=None, cursor=False):
    db = MagicMock()
    base = _chain(db, "table", "select", "eq")
    if cursor:
        leaf = _chain(base, "or_", "order", "order", "limit")
    else:
        leaf = _chain(base, "gt", "order", "order", "limit")

    if error is not None:
        leaf.execute.side_effect = error
    else:
        leaf.execute.return_value = MagicMock(data=data, count=count)
    return db


def _fake_like_db(data=None, error=None):
    # table().upsert().select().execute(): the like with the catalog track spread into the row.
    db = MagicMock()
    query = db.table.return_value.upsert.return_value.select.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_catalog_db(read=None, upsert=None, upsert_error=None, read_error=None):
    # table().select().eq().execute() reads the catalog duration;
    # table().upsert().execute() writes the track.
    db = MagicMock()
    reader = db.table.return_value.select.return_value.eq.return_value
    if read_error is not None:
        reader.execute.side_effect = read_error
    else:
        reader.execute.return_value = MagicMock(data=read if read is not None else [])
    writer = db.table.return_value.upsert.return_value
    if upsert_error is not None:
        writer.execute.side_effect = upsert_error
    else:
        writer.execute.return_value = MagicMock(data=[{}] if upsert is None else upsert)
    return db


def _use_like_dbs(like_data=None, like_error=None, catalog=None):
    # Separate mocks for the user's client and the service-role client.
    user_db = _fake_like_db(data=like_data, error=like_error)
    catalog_db = catalog if catalog is not None else _fake_catalog_db()
    _use_db(user_db)
    _use_catalog_db(catalog_db)
    return user_db, catalog_db


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
    db = _fake_list_db(data=[_ROW_LIKE], count=1)
    _use_db(db)
    _use_auth()

    response = client.get("/likes")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "items": [_ROW_LIKE],
        "page": {
            "limit": 50,
            "next_cursor": None,
            "has_more": False,
            "total": 1,
        },
    }
    assert db.table.return_value.select.call_args.args == (_COLUMNS,)
    assert db.table.return_value.select.call_args.kwargs["count"] == "exact"
    base = _chain(db, "table", "select", "eq")
    base.is_.assert_called_once_with("deleted_at", "null")
    query = base.is_.return_value
    query.order.assert_called_once_with("created_at", desc=False)
    query.order.return_value.order.assert_called_once_with("track_id", desc=False)
    query.order.return_value.order.return_value.limit.assert_called_once_with(51)


def test_empty_likes_returns_an_empty_first_page():
    _use_db(_fake_list_db(data=[], count=0))
    _use_auth()

    response = client.get("/likes")

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


def test_list_scopes_query_to_authenticated_user():
    db = _fake_list_db(data=[_ROW_LIKE], count=1)
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


def test_list_row_without_its_catalog_track_returns_upstream_error():
    # What PostgREST returns with the spread and a null relation.
    row = {**_ROW_LIKE, **dict.fromkeys(_TRACK_FIELDS)}
    _use_db(_fake_list_db(data=[row], count=1))
    _use_auth()

    response = client.get("/likes")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_unauthenticated_list_request_returns_unauthorized():
    response = client.get("/likes")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_list_first_page_has_more_true_with_limit():
    db = _fake_list_db(data=[_ROW_LIKE, _ROW_LIKE_2], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/likes", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_ROW_LIKE]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert body["page"]["total"] == 2
    query = _chain(db, "table", "select", "eq", "is_")
    query.order.return_value.order.return_value.limit.assert_called_once_with(2)


def test_list_next_page_via_cursor_returns_remaining_items_without_repeats():
    first_db = _fake_list_db(data=[_ROW_LIKE, _ROW_LIKE_2], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get("/likes", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_db(data=[_ROW_LIKE_2], count=None, cursor=True)
    _use_db(second_db)

    response = client.get("/likes", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_ROW_LIKE_2]
    assert body["page"]["total"] is None
    assert body["page"]["has_more"] is False
    assert body["page"]["next_cursor"] is None

    base = _chain(second_db, "table", "select", "eq", "is_")
    expected_cursor = decode_cursor(next_cursor, _LIST_SORT)
    base.or_.assert_called_once_with(keyset_filter(_LIST_SORT, expected_cursor))
    select_call = second_db.table.return_value.select.call_args
    assert select_call.kwargs["count"] is None


def test_list_invalid_cursor_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/likes", params={"cursor": "???"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


@pytest.mark.parametrize("limit", [0, -1, 101, "abc"])
def test_list_invalid_limit_returns_invalid_request(limit):
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/likes", params={"limit": limit})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


# --- POST /likes --------------------------------------------------------


def test_like_track_success():
    user_db, _ = _use_like_dbs(like_data=[_ROW_LIKE])
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == _ROW_LIKE
    user_db.table.return_value.upsert.return_value.select.assert_called_once_with(
        _COLUMNS
    )


def test_like_track_upsert_always_clears_deleted_at_to_revive():
    user_db, _ = _use_like_dbs(like_data=[_ROW_LIKE])
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 200
    user_db.table.assert_called_once_with("user_likes")
    user_db.table.return_value.upsert.assert_called_once_with(
        {"user_id": _USER_ID, "track_id": "t1", "deleted_at": None},
        on_conflict="user_id,track_id",
    )


def test_like_track_writes_the_catalog_with_the_service_role_client():
    user_db, catalog_db = _use_like_dbs(like_data=[_ROW_LIKE])
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 200
    catalog_db.table.assert_called_once_with("tracks")
    catalog_db.table.return_value.upsert.assert_called_once_with(
        _ADD_BODY, on_conflict="track_id"
    )
    user_db.table.assert_called_once_with("user_likes")


def test_like_track_without_duration_keeps_the_catalog_duration():
    body = {k: v for k, v in _ADD_BODY.items() if k != "duration_seconds"}
    _, catalog_db = _use_like_dbs(
        like_data=[_ROW_LIKE],
        catalog=_fake_catalog_db(read=[{"duration_seconds": 300}]),
    )
    _use_auth()

    response = client.post("/likes", json=body)

    assert response.status_code == 200
    reader = catalog_db.table.return_value.select
    reader.assert_called_once_with("duration_seconds")
    reader.return_value.eq.assert_called_once_with("track_id", "t1")
    catalog_db.table.return_value.upsert.assert_called_once_with(
        {**body, "duration_seconds": 300}, on_conflict="track_id"
    )


def test_like_track_without_duration_for_a_track_not_in_the_catalog_returns_invalid_request():
    body = {k: v for k, v in _ADD_BODY.items() if k != "duration_seconds"}
    user_db, catalog_db = _use_like_dbs(
        like_data=[_ROW_LIKE], catalog=_fake_catalog_db(read=[])
    )
    _use_auth()

    response = client.post("/likes", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    catalog_db.table.return_value.upsert.assert_not_called()
    user_db.table.return_value.upsert.assert_not_called()


def test_like_track_catalog_read_failure_returns_upstream_error():
    body = {k: v for k, v in _ADD_BODY.items() if k != "duration_seconds"}
    user_db, _ = _use_like_dbs(
        catalog=_fake_catalog_db(read_error=APIError({"message": "connection refused"}))
    )
    _use_auth()

    response = client.post("/likes", json=body)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    user_db.table.return_value.upsert.assert_not_called()


def test_like_track_catalog_failure_returns_upstream_error_without_writing_the_like():
    user_db, _ = _use_like_dbs(
        like_data=[_ROW_LIKE],
        catalog=_fake_catalog_db(
            upsert_error=APIError({"message": "connection refused"})
        ),
    )
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    user_db.table.return_value.upsert.assert_not_called()


def test_like_track_catalog_upsert_without_returned_row_returns_upstream_error():
    user_db, _ = _use_like_dbs(
        like_data=[_ROW_LIKE], catalog=_fake_catalog_db(upsert=[])
    )
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    user_db.table.return_value.upsert.assert_not_called()


@pytest.mark.parametrize(
    "missing_field", ["track_id", "title", "album", "album_id", "thumbnail_url"]
)
def test_like_track_missing_required_field_returns_invalid_request(missing_field):
    db, catalog_db = _use_like_dbs()
    _use_auth()
    body = {k: v for k, v in _ADD_BODY.items() if k != missing_field}

    response = client.post("/likes", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()
    catalog_db.table.assert_not_called()


def test_like_track_empty_artists_returns_invalid_request():
    db, catalog_db = _use_like_dbs()
    _use_auth()
    body = {**_ADD_BODY, "artists": []}

    response = client.post("/likes", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()
    catalog_db.table.assert_not_called()


def test_like_track_does_not_accept_user_id_from_body():
    db, catalog_db = _use_like_dbs(like_data=[_ROW_LIKE])
    _use_auth()
    body = {**_ADD_BODY, "user_id": "attacker-id"}

    response = client.post("/likes", json=body)

    assert response.status_code == 200
    called_payload = db.table.return_value.upsert.call_args[0][0]
    assert called_payload["user_id"] == _USER_ID
    assert "user_id" not in catalog_db.table.return_value.upsert.call_args[0][0]


def test_like_track_without_returned_row_returns_upstream_error():
    _use_like_dbs(like_data=[])
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_like_track_upstream_failure_returns_upstream_error():
    _use_like_dbs(like_error=APIError({"message": "connection refused"}))
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_like_track_upstream_timeout_returns_upstream_timeout():
    _use_like_dbs(like_error=httpx.ReadTimeout("timed out"))
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
    db = _fake_sync_db(data=[_ROW_LIKE, _ROW_LIKE_DELETED], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["items"] == [_ROW_LIKE, _ROW_LIKE_DELETED]
    assert body["data"]["page"]["total"] == 2
    assert db.table.return_value.select.call_args.args == (_COLUMNS,)
    assert db.table.return_value.select.call_args.kwargs["count"] == "exact"
    query = _chain(db, "table", "select", "eq")
    query.gt.assert_called_once_with("updated_at", "2026-01-01T00:00:00+00:00")
    query.gt.return_value.order.assert_called_once_with("updated_at", desc=False)


def test_sync_without_since_and_without_cursor_returns_invalid_request():
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
    _use_db(_fake_sync_db(data=[], count=0))
    _use_auth()

    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["items"] == []


def test_sync_scopes_query_to_authenticated_user():
    db = _fake_sync_db(data=[_ROW_LIKE], count=1)
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


def test_sync_first_page_has_more_true_with_limit():
    db = _fake_sync_db(data=[_ROW_LIKE, _ROW_LIKE_2], count=2)
    _use_db(db)
    _use_auth()

    response = client.get(
        "/likes/sync",
        params={"since": "2026-01-01T00:00:00Z", "limit": 1},
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_ROW_LIKE]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert body["page"]["total"] == 2


def test_sync_with_cursor_does_not_require_since():
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", "t1", _SYNC_SORT)
    db = _fake_sync_db(data=[_ROW_LIKE_2], count=None, cursor=True)
    _use_db(db)
    _use_auth()

    response = client.get("/likes/sync", params={"cursor": cursor})

    assert response.status_code == 200
    assert db.table.return_value.select.call_args.kwargs["count"] is None
    base = _chain(db, "table", "select", "eq")
    base.gt.assert_not_called()
    base.or_.assert_called_once()


def test_sync_cursor_takes_precedence_over_since():
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", "t1", _SYNC_SORT)
    db = _fake_sync_db(data=[_ROW_LIKE_2], count=None, cursor=True)
    _use_db(db)
    _use_auth()

    response = client.get(
        "/likes/sync",
        params={"since": "2026-01-01T00:00:00Z", "cursor": cursor},
    )

    assert response.status_code == 200
    base = _chain(db, "table", "select", "eq")
    base.gt.assert_not_called()
    base.or_.assert_called_once()


def test_sync_invalid_cursor_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/likes/sync", params={"cursor": "???"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


# --- Cache-Control ---------------------------------------------------------


def test_list_likes_sends_private_no_cache():
    _use_db(_fake_list_db(data=[_ROW_LIKE], count=1))
    _use_auth()

    response = client.get("/likes")

    assert response.status_code == 200
    assert response.headers.get_list("cache-control") == ["private, no-cache"]


def test_like_track_sends_private_no_cache():
    # A write is user data too: the rule is per domain, not per method.
    _use_like_dbs(like_data=[_ROW_LIKE])
    _use_auth()

    response = client.post("/likes", json=_ADD_BODY)

    assert response.status_code == 200
    assert response.headers.get_list("cache-control") == ["private, no-cache"]


# --- Thumbnails at 544 x 544, smart crop -----------------------------------

_SQUARE = "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"
_URL_60 = "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj"


def test_list_likes_returns_thumbnail_url_at_544_smart_crop():
    row = {**_ROW_LIKE, "thumbnail_url": _URL_60}
    _use_db(_fake_list_db(data=[row], count=1))
    _use_auth()

    response = client.get("/likes")

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["thumbnail_url"] == _SQUARE


def test_sync_returns_thumbnail_url_at_544_smart_crop():
    row = {**_ROW_LIKE, "thumbnail_url": _URL_60}
    _use_db(_fake_sync_db(data=[row], count=1))
    _use_auth()

    response = client.get("/likes/sync", params={"since": "2026-01-01T00:00:00Z"})

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["thumbnail_url"] == _SQUARE


def test_like_track_returns_thumbnail_url_at_544_and_stores_it_as_sent():
    body = {**_ADD_BODY, "thumbnail_url": _URL_60}
    _, catalog_db = _use_like_dbs(like_data=[{**_ROW_LIKE, "thumbnail_url": _URL_60}])
    _use_auth()

    response = client.post("/likes", json=body)

    assert response.status_code == 200
    assert response.json()["data"]["thumbnail_url"] == _SQUARE
    catalog_db.table.return_value.upsert.assert_called_once_with(
        body, on_conflict="track_id"
    )
