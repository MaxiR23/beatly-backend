# test/routes/test_playlists.py
#
# Tests for the playlist endpoints.
#
# Tested:
# - POST /playlists creates a playlist owned by the authenticated user
# - POST /playlists takes owner_id from the token and ignores any
#   owner_id sent in the body
# - POST /playlists defaults is_public to false
# - POST /playlists rejects a missing, blank or over-long title with 422
#   invalid_request, without reaching the database
# - POST /playlists returns 502 when the insert returns no row
# - GET /playlists lists the caller's playlists, cursor-paginated:
#   data.items + data.page, created_at descending with id breaking ties
# - has_more/next_cursor derive from the limit+1 probe row, and total is
#   present (exact) only on the first page, null on a cursored one
# - A next_cursor round-trips: the following page continues where the
#   previous one ended, without repeating a playlist
# - Returns 200 ok:true with an empty first page when the caller has no
#   playlists — not "no_playlists"
# - A garbage cursor, and one emitted by another paginated endpoint, are
#   both 422 invalid_cursor without reaching the database
# - Returns 422 invalid_request for a limit outside 1..100, without
#   reaching the database
# - GET /playlists is scoped to the authenticated user's id
# - A stored null is_public reads back as false rather than failing
#   validation, on both the list and the detail endpoint
# - GET /playlists/{id} returns the playlist with its tracks ordered by
#   position
# - GET /playlists/{id} joins tracks on the track uuid, not the provider
#   id the curated genre path uses
# - GET /playlists/{id} reads the track catalog in batches, so a long
#   playlist does not build a URI the database rejects, and merges the
#   batched results in position order
# - GET /playlists/{id} returns 502 for a track row with an empty
#   artists list
# - GET /playlists/{id} on a playlist with no tracks is ok:true with an
#   empty track list, not an empty state
# - GET /playlists/{id} reports total_count and has_more when the track
#   list is capped
# - GET /playlists/{id} returns total_duration_seconds calculated by the
#   database, not summed in Python over the tracks returned
# - total_duration_seconds covers every track in the playlist, not just
#   the ones read into tracks — it stays correct above the 1000-track cap
# - An empty playlist reports total_duration_seconds: 0, not null
# - A failure, a timeout, or a non-numeric payload from the duration RPC
#   are 502/504, the same as any other upstream failure on this endpoint
# - A boolean payload from the duration RPC is 502, not 200 with
#   total_duration_seconds: 1 — bool is a subclass of int in Python
# - GET /playlists/{id} returns 404 playlist_not_found for an unknown
#   playlist and for one owned by another user
# - PATCH /playlists/{id} updates title, description and is_public
# - PATCH /playlists/{id} with a partial body only sends what was set,
#   and never sends updated_at
# - PATCH /playlists/{id} clears description with an explicit null
# - PATCH /playlists/{id} with an empty body returns the current state
#   without writing
# - PATCH /playlists/{id} rejects an explicit null, blank or over-long
#   title with 422, without reaching the database
# - PATCH /playlists/{id} returns 404 playlist_not_found for an unknown
#   playlist, one owned by another user, and one deleted between the
#   read and the write
# - DELETE /playlists/{id} deletes the playlist
# - DELETE /playlists/{id} returns 404 playlist_not_found for an unknown
#   playlist, one owned by another user, and one deleted between the
#   read and the write
# - DELETE /playlists/{id} does not push the ownership rule into the
#   query, so can_edit stays the single permission check
# - POST /playlists/{id}/tracks upserts the track metadata on track_id
#   and links it through the add_playlist_track RPC, which assigns the
#   position; the service never reads or writes playlist_tracks itself
# - The response carries the catalog uuid, not the playlist_tracks row id
#   the RPC returns
# - An RPC answering track_already_in_playlist is 409, a playlist deleted
#   between the permission check and the write is 404, and any other
#   refusal is 502
# - POST /playlists/{id}/tracks rejects a missing duration_seconds or an
#   empty artists list with 422, without reaching the database
# - POST /playlists/{id}/tracks/bulk links the whole batch with one
#   add_playlist_tracks_bulk call, in the order the tracks were sent, and
#   reports added and skipped
# - The adding user comes from the token
# - The catalog upsert is the only read of provider ids: nothing is read
#   before the write, neither the ids nor the existing links, since what
#   is skipped is the RPC's decision
# - A track repeated inside one batch is added once and counted as
#   skipped for the rest, and reaches neither the catalog write nor the
#   RPC a second time
# - Tracks the RPC skipped are reported as skipped, and a batch it skips
#   whole is added:0 rather than an error
# - A playlist deleted between the permission check and the write is 404,
#   and any other refusal by the RPC is 502
# - A full 200-track batch still goes out in one call
# - An empty batch and one over 200 tracks are both 422, without
#   reaching the database or the RPC
# - DELETE /playlists/{id}/tracks/{track_id} removes the link with one
#   remove_playlist_track call, passing the provider id straight through:
#   the service reads no other table and writes nothing itself
# - Removing a track that is not in the playlist, or one the catalog
#   does not know, is 200 rather than an error
# - A playlist deleted between the permission check and the write is 404,
#   and any other refusal by the RPC is 502
# - can_edit still decides before the RPC is reached
# - POST /playlists/{id}/move-track calls the move_playlist_track RPC
#   with 1-based indices
# - A position of zero or past the end of the playlist is 422 and the
#   RPC is never called, so the RPC's clamp is never relied on
# - An RPC answering ok:false is 502, not a silent success
# - GET /playlists/owned-with-track/{track_id} returns the caller's
#   playlist ids for a provider id, passing the user id from the token
# - A track in none of the caller's playlists is ok:true with an empty
#   list, not an empty state
# - GET /playlists/liked returns a PlaylistDetail-shaped virtual playlist
#   sourced from active user_likes rather than playlist_tracks, with id
#   and title both the literal "liked"
# - GET /playlists/liked is scoped to the caller's user_id, filters
#   deleted_at is null, and orders by created_at ascending with track_id
#   breaking ties
# - GET /playlists/liked joins the catalog on tracks.track_id (the
#   provider id), not tracks.id
# - GET /playlists/liked reads the catalog in batches and merges the
#   results in like order, same as GET /playlists/{playlist_id}
# - A user with no active likes gets ok:true with tracks: [], total_count:
#   0, has_more: false, total_duration_seconds: 0, and created_at ==
#   updated_at, without querying for the most recent like
# - total_count and has_more report the cap correctly when the like count
#   exceeds the tracks read
# - updated_at is the created_at of the most recent active like, from its
#   own query ordered created_at descending, not derived from the (possibly
#   capped) rows already read; created_at is the oldest row read, which the
#   cap cannot have truncated
# - total_duration_seconds comes from get_liked_tracks_duration_total, not
#   summed in Python, and is not limited by the cap on tracks read
# - A failure or a timeout on any of the four queries this endpoint makes
#   (likes read, most-recent-like probe, catalog read, duration RPC) is
#   502/504
# - A non-numeric duration RPC payload, a liked track_id missing from the
#   catalog, and a catalog row with an empty artists list are all 502
# - GET /playlists/liked returns 401 without an Authorization header
# - GET /playlists/liked is matched before /{playlist_id}: it never reads
#   the playlists table
# - Every track endpoint refuses a playlist the caller cannot edit with
#   404 playlist_not_found, before touching any other table
# - A malformed playlist id is 422 invalid_request, without reaching the
#   database
# - Every endpoint returns 401 without an Authorization header and
#   502/504 when the database fails or times out
#
# What is covered:
# - Happy path, expected empty state, cursor pagination, partial update,
#   invalid input, playlist not found on read, update and delete,
#   permission scoping, duplicate conflict, idempotent removal, batch
#   deduplication and skipping, position validation, upstream failure,
#   upstream timeout, unauthenticated access
#
# Run with: pytest test/routes/test_playlists.py -v
#
# SEE: routes/playlists.py, services/playlist_service.py

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.auth import get_current_user_id
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
_OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
_PLAYLIST_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_OLDER_PLAYLIST_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

# Mirrors the sort key declared in services/playlist_service.py, used only
# to build and decode cursors for these tests — never imported from the
# service, so the tests fail if the two drift apart. _FOREIGN_SORT is
# GET /library's, declared here for the same reason: it is what another
# paginated endpoint's cursor is tagged with.
_LIST_SORT = SortKey("created_at", ValueType.TIMESTAMP)
_FOREIGN_SORT = SortKey("added_at", ValueType.TIMESTAMP)

_TRACK_ONE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_TRACK_TWO_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

# What add_playlist_track returns as "id": the playlist_tracks row, not the
# catalog uuid. Deliberately different from both track ids above, so a test
# can tell which one reached the response.
_LINK_ROW_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

_PLAYLIST_ROW = {
    "id": _PLAYLIST_ID,
    "owner_id": _USER_ID,
    "title": "Road trip",
    "description": "Songs for the car",
    "is_public": False,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}

_OTHER_PLAYLIST_ROW = {**_PLAYLIST_ROW, "owner_id": _OTHER_USER_ID}

# A second page's worth: older than _PLAYLIST_ROW and with a different id,
# so created_at descending is observable and the cursor built from either
# row is distinguishable.
_OLDER_PLAYLIST_ROW = {
    **_PLAYLIST_ROW,
    "id": _OLDER_PLAYLIST_ID,
    "title": "Old mixtape",
    "created_at": "2025-12-01T00:00:00Z",
    "updated_at": "2025-12-01T00:00:00Z",
}

_TRACK_ONE = {
    "id": _TRACK_ONE_ID,
    "track_id": "t1",
    "title": "Song A",
    "artists": [{"id": "a1", "name": "Artist One"}],
    "album": "Album A",
    "album_id": "album-a",
    "duration_seconds": 180,
    "thumbnail_url": "https://example.com/a.png",
}

_TRACK_TWO = {
    "id": _TRACK_TWO_ID,
    "track_id": "t2",
    "title": "Song B",
    "artists": [{"id": "a2", "name": "Artist Two"}],
    "album": "Album B",
    "album_id": "album-b",
    "duration_seconds": 240,
    "thumbnail_url": "https://example.com/b.png",
}


# The request body is the catalog row without the uuid the database
# assigns, so deriving it keeps the two from drifting apart.
_ADD_ONE_BODY = {key: value for key, value in _TRACK_ONE.items() if key != "id"}
_ADD_TWO_BODY = {key: value for key, value in _TRACK_TWO.items() if key != "id"}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


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


def _fake_detail_db(
    playlist_rows=None,
    entry_rows=None,
    track_rows=None,
    total_count=None,
    duration_total=None,
    playlist_error=None,
    entries_error=None,
    tracks_error=None,
    duration_error=None,
):
    if entry_rows is None:
        entry_rows = []
    if track_rows is None:
        track_rows = []
    if total_count is None:
        total_count = len(entry_rows)

    db = MagicMock()
    # Memoized so a test can assert against the same table mock the
    # request used, via db.tables["tracks"].
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]

        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "playlists":
            _pin_playlist(table_mock, playlist_rows, playlist_error)
        elif name == "playlist_tracks":
            query = table_mock.select.return_value.eq.return_value.order.return_value.limit.return_value
            if entries_error is not None:
                query.execute.side_effect = entries_error
            else:
                query.execute.return_value = MagicMock(
                    data=entry_rows, count=total_count
                )
        elif name == "tracks":
            query = table_mock.select.return_value.in_.return_value
            if tracks_error is not None:
                query.execute.side_effect = tracks_error
            else:
                query.execute.return_value = MagicMock(data=track_rows)
        return table_mock

    db.table.side_effect = table_side_effect
    db.tables = tables

    # Derived from track_rows, not entry_rows: that is what get_playlist_
    # duration_total would answer for a correct database when the two
    # correspond 1:1, which is the case for almost every test here. The
    # .get(..., 0) tolerates the deliberately malformed rows some tests use.
    if duration_total is None:
        duration_total = sum(row.get("duration_seconds", 0) for row in track_rows)
    _pin(db.rpc.return_value, data=duration_total, error=duration_error)

    return db


def _batched_detail_rows(count):
    # A playlist long enough that the catalog read has to be split, and the
    # catalog rows behind it. Positions are the entry index, so a test can
    # assert the merged result came back in order.
    entry_rows = [
        {"track_id": f"{index:08d}-0000-0000-0000-000000000000", "position": index}
        for index in range(count)
    ]
    track_rows = [
        {**_TRACK_ONE, "id": row["track_id"], "track_id": f"t{row['position']}"}
        for row in entry_rows
    ]
    return entry_rows, track_rows


def _fake_update_db(get_data=None, get_error=None, update_data=None, update_error=None):
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


def _fake_delete_db(get_data=None, get_error=None, delete_data=None, delete_error=None):
    db = MagicMock()
    table = db.table.return_value

    get_query = table.select.return_value.eq.return_value
    if get_error is not None:
        get_query.execute.side_effect = get_error
    else:
        get_query.execute.return_value = MagicMock(data=get_data)

    delete_query = table.delete.return_value.eq.return_value
    if delete_error is not None:
        delete_query.execute.side_effect = delete_error
    else:
        delete_query.execute.return_value = MagicMock(data=delete_data)

    return db


def _pin(query, data=None, error=None, count=None):
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(
            data=[] if data is None else data, count=count
        )


def _pin_playlist(table, rows=None, error=None):
    # Every endpoint below starts with the same read: the playlists row the
    # permission check runs on. Defaults to the caller's own playlist, so a
    # test only names it when the row is the point.
    _pin(
        table.select.return_value.eq.return_value,
        data=[_PLAYLIST_ROW] if rows is None else rows,
        error=error,
    )


def _fake_multi_table_db(configure):
    # The track endpoints touch playlists, tracks and playlist_tracks in one
    # request. Memoized like _fake_detail_db, so a test can assert against
    # the same table mock the request used, via db.tables["tracks"], and can
    # check that a table was never reached at all.
    db = MagicMock()
    tables = {}

    def table_side_effect(name):
        if name not in tables:
            table_mock = MagicMock()
            tables[name] = table_mock
            configure(name, table_mock)
        return tables[name]

    db.table.side_effect = table_side_effect
    db.tables = tables
    return db


def _fake_add_db(
    playlist_rows=None,
    upsert_rows=None,
    rpc_data=None,
    playlist_error=None,
    upsert_error=None,
    rpc_error=None,
):
    # Both add endpoints touch the same two tables and one RPC: the playlist
    # for the permission check, the catalog upsert, then the link. rpc_data
    # is the payload the RPC answers with, in the shape of whichever of the
    # two the test is exercising.
    if upsert_rows is None:
        upsert_rows = [_TRACK_ONE]
    if rpc_data is None:
        rpc_data = {"ok": True, "id": _LINK_ROW_ID, "position": 1}

    def configure(name, table):
        if name == "playlists":
            _pin_playlist(table, playlist_rows, playlist_error)
        elif name == "tracks":
            _pin(table.upsert.return_value, data=upsert_rows, error=upsert_error)

    db = _fake_multi_table_db(configure)
    _pin(db.rpc.return_value, data=rpc_data, error=rpc_error)
    return db


def _bulk_rpc(added, skipped=0):
    # What add_playlist_tracks_bulk answers with. Its skipped counts only the
    # tracks already in the playlist: the array it is sent is deduplicated.
    return {"ok": True, "added": added, "skipped": skipped}


def _bulk_call(db):
    # The arguments of the one add_playlist_tracks_bulk call the endpoint
    # makes.
    return db.rpc.call_args.args


def _bulk_batch(count):
    # A batch of distinct tracks, and the catalog rows they upsert to.
    bodies = [{**_ADD_ONE_BODY, "track_id": f"t{index}"} for index in range(count)]
    rows = [
        {
            **_TRACK_ONE,
            "id": f"{index:08d}-0000-0000-0000-000000000000",
            "track_id": f"t{index}",
        }
        for index in range(count)
    ]
    return bodies, rows


def _fake_remove_db(
    playlist_rows=None,
    rpc_data=None,
    playlist_error=None,
    rpc_error=None,
):
    # Removing reads the playlist for the permission check and then calls
    # remove_playlist_track. It touches no other table: the RPC resolves the
    # provider id itself.
    if rpc_data is None:
        rpc_data = {"ok": True, "deleted": 1}

    def configure(name, table):
        if name == "playlists":
            _pin_playlist(table, playlist_rows, playlist_error)

    db = _fake_multi_table_db(configure)
    _pin(db.rpc.return_value, data=rpc_data, error=rpc_error)
    return db


def _fake_move_db(
    playlist_rows=None,
    track_count=2,
    rpc_data=None,
    playlist_error=None,
    count_error=None,
    rpc_error=None,
):
    if rpc_data is None:
        rpc_data = {"ok": True, "order": [_TRACK_TWO_ID, _TRACK_ONE_ID]}

    def configure(name, table):
        if name == "playlists":
            _pin_playlist(table, playlist_rows, playlist_error)
        elif name == "playlist_tracks":
            _pin(
                table.select.return_value.eq.return_value.limit.return_value,
                data=[],
                count=track_count,
                error=count_error,
            )

    db = _fake_multi_table_db(configure)
    _pin(db.rpc.return_value, data=rpc_data, error=rpc_error)
    return db


def _fake_rpc_db(data=None, error=None):
    db = MagicMock()
    _pin(db.rpc.return_value, data=data, error=error)
    return db


# --- POST /playlists --------------------------------------------------


def test_create_playlist_returns_created_playlist():
    _use_db(_fake_create_db(data=[_PLAYLIST_ROW]))
    _use_auth()

    response = client.post("/playlists", json={"title": "Road trip"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == _PLAYLIST_ROW


def test_create_playlist_sends_owner_id_from_token():
    db = _fake_create_db(data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    client.post(
        "/playlists",
        json={"title": "Road trip", "description": "Songs for the car"},
    )

    db.table.return_value.insert.assert_called_once_with(
        {
            "title": "Road trip",
            "description": "Songs for the car",
            "is_public": False,
            "owner_id": _USER_ID,
        }
    )


def test_create_playlist_ignores_owner_id_from_body():
    db = _fake_create_db(data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    client.post(
        "/playlists",
        json={"title": "Road trip", "owner_id": _OTHER_USER_ID},
    )

    payload = db.table.return_value.insert.call_args.args[0]
    assert payload["owner_id"] == _USER_ID


def test_create_playlist_defaults_is_public_to_false():
    db = _fake_create_db(data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    client.post("/playlists", json={"title": "Road trip"})

    payload = db.table.return_value.insert.call_args.args[0]
    assert payload["is_public"] is False


def test_create_playlist_missing_title_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.post("/playlists", json={"description": "no title"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_playlist_blank_title_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.post("/playlists", json={"title": ""})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_playlist_over_long_title_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.post("/playlists", json={"title": "x" * 201})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_create_playlist_without_returned_row_returns_upstream_error():
    _use_db(_fake_create_db(data=[]))
    _use_auth()

    response = client.post("/playlists", json={"title": "Road trip"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_create_playlist_upstream_failure_returns_upstream_error():
    _use_db(_fake_create_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post("/playlists", json={"title": "Road trip"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_create_playlist_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_create_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post("/playlists", json={"title": "Road trip"})

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_create_request_returns_unauthorized():
    response = client.post("/playlists", json={"title": "Road trip"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- GET /playlists ---------------------------------------------------


def test_returns_playlists_newest_first_with_exact_total():
    db = _fake_list_db(data=[_PLAYLIST_ROW, _OLDER_PLAYLIST_ROW], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "items": [_PLAYLIST_ROW, _OLDER_PLAYLIST_ROW],
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


def test_first_page_with_limit_has_more_and_next_cursor():
    db = _fake_list_db(data=[_PLAYLIST_ROW, _OLDER_PLAYLIST_ROW], count=2)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_PLAYLIST_ROW]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert body["page"]["total"] == 2
    query = _chain(db, "table", "select", "eq")
    query.order.return_value.order.return_value.limit.assert_called_once_with(2)


def test_next_page_via_cursor_returns_remaining_items_without_repeats():
    first_db = _fake_list_db(data=[_PLAYLIST_ROW, _OLDER_PLAYLIST_ROW], count=2)
    _use_db(first_db)
    _use_auth()
    first_response = client.get("/playlists", params={"limit": 1})
    next_cursor = first_response.json()["data"]["page"]["next_cursor"]

    second_db = _fake_list_db(data=[_OLDER_PLAYLIST_ROW], count=None, cursor=True)
    _use_db(second_db)

    response = client.get("/playlists", params={"limit": 1, "cursor": next_cursor})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == [_OLDER_PLAYLIST_ROW]
    assert body["page"]["total"] is None
    assert body["page"]["has_more"] is False
    assert body["page"]["next_cursor"] is None

    base = _chain(second_db, "table", "select", "eq")
    expected_cursor = decode_cursor(next_cursor, _LIST_SORT)
    base.or_.assert_called_once_with(keyset_filter(_LIST_SORT, expected_cursor))
    assert second_db.table.return_value.select.call_args.kwargs["count"] is None


def test_empty_playlists_is_an_empty_first_page():
    _use_db(_fake_list_db(data=[], count=0))
    _use_auth()

    response = client.get("/playlists")

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


def test_garbage_cursor_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/playlists", params={"cursor": "???"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


def test_cursor_from_another_endpoint_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    # Same value types as this endpoint's cursor, timestamp plus uuid: what
    # rejects it is the sort key tag it was emitted under, not the types.
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", _PLAYLIST_ID, _FOREIGN_SORT)

    response = client.get("/playlists", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


@pytest.mark.parametrize("limit", [0, -5, 101, "abc"])
def test_invalid_limit_returns_invalid_request(limit):
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/playlists", params={"limit": limit})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_list_reads_a_null_is_public_as_false():
    _use_db(_fake_list_db(data=[{**_PLAYLIST_ROW, "is_public": None}], count=1))
    _use_auth()

    response = client.get("/playlists")

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["is_public"] is False


def test_list_scopes_query_to_authenticated_user():
    db = _fake_list_db(data=[], count=0)
    _use_db(db)
    _use_auth(user_id=_OTHER_USER_ID)

    client.get("/playlists")

    db.table.return_value.select.return_value.eq.assert_called_once_with(
        "owner_id", _OTHER_USER_ID
    )


def test_list_upstream_failure_returns_upstream_error():
    _use_db(_fake_list_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/playlists")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_list_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_list_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/playlists")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_list_request_returns_unauthorized():
    response = client.get("/playlists")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- GET /playlists/liked ----------------------------------------------

# Two active likes, oldest first -- the order the endpoint returns tracks
# in. Distinct timestamps so created_at/updated_at derivation is
# observable.
_LIKE_ONE_ROW = {
    "track_id": _TRACK_ONE["track_id"],
    "created_at": "2026-01-01T00:00:00Z",
}
_LIKE_TWO_ROW = {
    "track_id": _TRACK_TWO["track_id"],
    "created_at": "2026-01-02T00:00:00Z",
}


def _batched_liked_rows(count):
    # A liked list long enough that the catalog read has to be split, and
    # the catalog rows behind it. created_at increases with the index, so
    # the fixture's own "most recent like" default matches the last row.
    entry_rows = [
        {"track_id": f"t{index}", "created_at": f"2026-01-01T00:00:00.{index:06d}Z"}
        for index in range(count)
    ]
    track_rows = [
        {
            **_TRACK_ONE,
            "id": f"{index:08d}-0000-0000-0000-000000000000",
            "track_id": f"t{index}",
        }
        for index in range(count)
    ]
    return entry_rows, track_rows


def _fake_liked_db(
    entry_rows=None,
    track_rows=None,
    total_count=None,
    latest_created_at=None,
    duration_total=None,
    entries_error=None,
    latest_error=None,
    tracks_error=None,
    duration_error=None,
):
    if entry_rows is None:
        entry_rows = []
    if track_rows is None:
        track_rows = []
    if total_count is None:
        total_count = len(entry_rows)

    db = MagicMock()
    # Memoized so a test can assert against the same table mock the
    # request used, via db.tables["user_likes"] / db.tables["tracks"].
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]

        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "user_likes":
            # Both reads hang off the same table mock and share the same
            # select().eq().is_() prefix; they are told apart by the tail
            # of the chain: order().order().limit() for the main read,
            # order().limit() for the most-recent-like probe.
            base = _chain(table_mock, "select", "eq", "is_")
            main_leaf = _chain(base, "order", "order", "limit")
            if entries_error is not None:
                main_leaf.execute.side_effect = entries_error
            else:
                main_leaf.execute.return_value = MagicMock(
                    data=entry_rows, count=total_count
                )

            latest_leaf = _chain(base, "order", "limit")
            if latest_error is not None:
                latest_leaf.execute.side_effect = latest_error
            else:
                resolved_latest = latest_created_at
                if resolved_latest is None and entry_rows:
                    resolved_latest = entry_rows[-1]["created_at"]
                latest_data = (
                    [] if resolved_latest is None else [{"created_at": resolved_latest}]
                )
                latest_leaf.execute.return_value = MagicMock(data=latest_data)
        elif name == "tracks":
            query = table_mock.select.return_value.in_.return_value
            if tracks_error is not None:
                query.execute.side_effect = tracks_error
            else:
                query.execute.return_value = MagicMock(data=track_rows)
        return table_mock

    db.table.side_effect = table_side_effect
    db.tables = tables

    # Derived from track_rows, not entry_rows, mirroring _fake_detail_db.
    if duration_total is None:
        duration_total = sum(row.get("duration_seconds", 0) for row in track_rows)
    _pin(db.rpc.return_value, data=duration_total, error=duration_error)

    return db


def test_get_liked_playlist_returns_tracks_in_like_order():
    db = _fake_liked_db(
        entry_rows=[_LIKE_ONE_ROW, _LIKE_TWO_ROW],
        track_rows=[_TRACK_ONE, _TRACK_TWO],
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["id"] == "liked"
    assert data["title"] == "liked"
    assert data["owner_id"] == _USER_ID
    assert data["is_public"] is False
    assert data["description"] is None
    assert data["tracks"] == [
        {**_TRACK_ONE, "position": 1},
        {**_TRACK_TWO, "position": 2},
    ]
    assert data["total_count"] == 2
    assert data["has_more"] is False


def test_get_liked_playlist_is_scoped_and_ordered_by_created_at_then_track_id():
    db = _fake_liked_db(
        entry_rows=[_LIKE_ONE_ROW, _LIKE_TWO_ROW],
        track_rows=[_TRACK_ONE, _TRACK_TWO],
    )
    _use_db(db)
    _use_auth()

    client.get("/playlists/liked")

    likes_table = db.tables["user_likes"]
    likes_table.select.return_value.eq.assert_any_call("user_id", _USER_ID)
    likes_table.select.return_value.eq.return_value.is_.assert_any_call(
        "deleted_at", "null"
    )

    is_node = _chain(likes_table, "select", "eq", "is_")
    is_node.order.assert_any_call("created_at")
    is_node.order.return_value.order.assert_called_once_with("track_id")
    is_node.order.return_value.order.return_value.limit.assert_called_once_with(1000)
    is_node.order.assert_any_call("created_at", desc=True)


def test_get_liked_playlist_joins_tracks_on_the_provider_id():
    db = _fake_liked_db(
        entry_rows=[_LIKE_ONE_ROW],
        track_rows=[_TRACK_ONE],
    )
    _use_db(db)
    _use_auth()

    client.get("/playlists/liked")

    tracks_table = db.tables["tracks"]
    tracks_table.select.return_value.in_.assert_called_once_with(
        "track_id", [_TRACK_ONE["track_id"]]
    )


def test_get_liked_playlist_fetches_tracks_in_batches():
    entry_rows, track_rows = _batched_liked_rows(200)
    db = _fake_liked_db(entry_rows=entry_rows, track_rows=track_rows)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 200
    in_mock = db.tables["tracks"].select.return_value.in_
    assert in_mock.call_count == 2
    assert len(in_mock.call_args_list[0].args[1]) == 150
    assert len(in_mock.call_args_list[1].args[1]) == 50


def test_get_liked_playlist_merges_batched_track_results_in_like_order():
    entry_rows, track_rows = _batched_liked_rows(200)
    db = _fake_liked_db(entry_rows=entry_rows, track_rows=track_rows)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    tracks = response.json()["data"]["tracks"]
    assert len(tracks) == 200
    assert [track["position"] for track in tracks] == list(range(1, 201))
    assert [track["track_id"] for track in tracks] == [
        row["track_id"] for row in entry_rows
    ]


def test_get_liked_playlist_with_no_likes_returns_empty_state():
    db = _fake_liked_db(entry_rows=[], total_count=0, duration_total=0)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["tracks"] == []
    assert data["total_count"] == 0
    assert data["has_more"] is False
    assert data["total_duration_seconds"] == 0
    assert data["created_at"] == data["updated_at"]

    latest_leaf = _chain(
        db.tables["user_likes"], "select", "eq", "is_", "order", "limit"
    )
    latest_leaf.execute.assert_not_called()


def test_get_liked_playlist_over_the_cap_reports_has_more():
    db = _fake_liked_db(
        entry_rows=[_LIKE_ONE_ROW],
        track_rows=[_TRACK_ONE],
        total_count=1500,
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    body = response.json()
    assert body["data"]["total_count"] == 1500
    assert body["data"]["has_more"] is True
    assert len(body["data"]["tracks"]) == 1


def test_get_liked_playlist_updated_at_is_not_derived_from_the_rows_read():
    # created_at of the rows read is old; the descending probe answers a
    # newer one the cap left out of the main read. updated_at must be the
    # probe's answer, and created_at must stay the oldest row read -- the
    # test the naive "derive both from tracks" implementation fails.
    db = _fake_liked_db(
        entry_rows=[_LIKE_ONE_ROW, _LIKE_TWO_ROW],
        track_rows=[_TRACK_ONE, _TRACK_TWO],
        latest_created_at="2027-01-01T00:00:00Z",
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    data = response.json()["data"]
    assert data["created_at"] == _LIKE_ONE_ROW["created_at"]
    assert data["updated_at"] == "2027-01-01T00:00:00Z"


def test_get_liked_playlist_duration_total_comes_from_the_database():
    db = _fake_liked_db(
        entry_rows=[_LIKE_ONE_ROW, _LIKE_TWO_ROW],
        track_rows=[_TRACK_ONE, _TRACK_TWO],
        duration_total=999,
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.json()["data"]["total_duration_seconds"] == 999
    db.rpc.assert_called_once_with(
        "get_liked_tracks_duration_total", {"p_user_id": _USER_ID}
    )


def test_get_liked_playlist_duration_total_is_not_limited_to_the_tracks_read():
    db = _fake_liked_db(
        entry_rows=[_LIKE_ONE_ROW],
        track_rows=[_TRACK_ONE],
        total_count=1500,
        duration_total=270000,
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    body = response.json()
    assert body["data"]["total_duration_seconds"] == 270000
    assert len(body["data"]["tracks"]) == 1


def test_get_liked_playlist_entries_failure_returns_upstream_error():
    _use_db(_fake_liked_db(entries_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_entries_timeout_returns_upstream_timeout():
    _use_db(_fake_liked_db(entries_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_liked_playlist_latest_like_failure_returns_upstream_error():
    _use_db(
        _fake_liked_db(
            entry_rows=[_LIKE_ONE_ROW],
            track_rows=[_TRACK_ONE],
            latest_error=APIError({"message": "connection refused"}),
        )
    )
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_tracks_failure_returns_upstream_error():
    _use_db(
        _fake_liked_db(
            entry_rows=[_LIKE_ONE_ROW],
            tracks_error=APIError({"message": "connection refused"}),
        )
    )
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_tracks_timeout_returns_upstream_timeout():
    _use_db(
        _fake_liked_db(
            entry_rows=[_LIKE_ONE_ROW],
            tracks_error=httpx.ReadTimeout("timed out"),
        )
    )
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_liked_playlist_duration_failure_returns_upstream_error():
    _use_db(
        _fake_liked_db(
            entry_rows=[_LIKE_ONE_ROW],
            track_rows=[_TRACK_ONE],
            duration_error=APIError({"message": "connection refused"}),
        )
    )
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_duration_timeout_returns_upstream_timeout():
    _use_db(
        _fake_liked_db(
            entry_rows=[_LIKE_ONE_ROW],
            track_rows=[_TRACK_ONE],
            duration_error=httpx.ReadTimeout("timed out"),
        )
    )
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_liked_playlist_non_numeric_duration_returns_upstream_error():
    db = _fake_liked_db(
        entry_rows=[_LIKE_ONE_ROW],
        track_rows=[_TRACK_ONE],
    )
    db.rpc.return_value.execute.return_value = MagicMock(data=None)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_track_missing_from_catalog_returns_upstream_error():
    _use_db(
        _fake_liked_db(
            entry_rows=[_LIKE_ONE_ROW],
            track_rows=[],
        )
    )
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_empty_artists_returns_upstream_error():
    _use_db(
        _fake_liked_db(
            entry_rows=[_LIKE_ONE_ROW],
            track_rows=[{**_TRACK_ONE, "artists": []}],
        )
    )
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_unauthenticated_get_liked_playlist_returns_unauthorized():
    response = client.get("/playlists/liked")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_get_liked_playlist_route_takes_precedence_over_playlist_id():
    db = _fake_liked_db(entry_rows=[], total_count=0, duration_total=0)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == "liked"
    assert "playlists" not in db.tables


# --- GET /playlists/{playlist_id} -------------------------------------


def test_get_playlist_returns_tracks_ordered_by_position():
    _use_db(
        _fake_detail_db(
            entry_rows=[
                {"track_id": _TRACK_ONE_ID, "position": 1},
                {"track_id": _TRACK_TWO_ID, "position": 2},
            ],
            # Returned out of position order, on purpose.
            track_rows=[_TRACK_TWO, _TRACK_ONE],
        )
    )
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        **_PLAYLIST_ROW,
        "tracks": [
            {**_TRACK_ONE, "position": 1},
            {**_TRACK_TWO, "position": 2},
        ],
        "total_count": 2,
        "has_more": False,
        "total_duration_seconds": 420,
    }


def test_get_playlist_joins_tracks_on_track_uuid_not_provider_id():
    db = _fake_detail_db(
        entry_rows=[{"track_id": _TRACK_ONE_ID, "position": 1}],
        track_rows=[_TRACK_ONE],
    )
    _use_db(db)
    _use_auth()

    client.get(f"/playlists/{_PLAYLIST_ID}")

    tracks_table = db.tables["tracks"]
    tracks_table.select.return_value.in_.assert_called_once_with("id", [_TRACK_ONE_ID])


def test_get_playlist_fetches_tracks_in_batches():
    # One in_ filter holding a full playlist's uuids builds a URI Supabase
    # rejects, so the catalog read is batched.
    entry_rows, track_rows = _batched_detail_rows(200)
    db = _fake_detail_db(entry_rows=entry_rows, track_rows=track_rows)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    in_mock = db.tables["tracks"].select.return_value.in_
    assert in_mock.call_count == 2
    assert len(in_mock.call_args_list[0].args[1]) == 150
    assert len(in_mock.call_args_list[1].args[1]) == 50


def test_get_playlist_merges_batched_track_results():
    entry_rows, track_rows = _batched_detail_rows(200)
    _use_db(_fake_detail_db(entry_rows=entry_rows, track_rows=track_rows))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    tracks = response.json()["data"]["tracks"]
    assert len(tracks) == 200
    assert [track["position"] for track in tracks] == list(range(200))


def test_get_playlist_empty_artists_returns_upstream_error():
    _use_db(
        _fake_detail_db(
            entry_rows=[{"track_id": _TRACK_ONE_ID, "position": 1}],
            track_rows=[{**_TRACK_ONE, "artists": []}],
        )
    )
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_with_no_tracks_returns_empty_track_list():
    _use_db(_fake_detail_db(entry_rows=[], duration_total=0))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["tracks"] == []
    assert body["data"]["total_count"] == 0
    assert body["data"]["has_more"] is False
    assert body["data"]["total_duration_seconds"] == 0


def test_get_playlist_over_the_cap_reports_has_more():
    _use_db(
        _fake_detail_db(
            entry_rows=[{"track_id": _TRACK_ONE_ID, "position": 1}],
            track_rows=[_TRACK_ONE],
            total_count=1500,
        )
    )
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    body = response.json()
    assert body["data"]["total_count"] == 1500
    assert body["data"]["has_more"] is True
    assert len(body["data"]["tracks"]) == 1


def test_get_playlist_asks_the_database_for_the_duration_total():
    db = _fake_detail_db(
        entry_rows=[
            {"track_id": _TRACK_ONE_ID, "position": 1},
            {"track_id": _TRACK_TWO_ID, "position": 2},
        ],
        track_rows=[_TRACK_ONE, _TRACK_TWO],
        duration_total=999,
    )
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    body = response.json()
    assert body["data"]["total_duration_seconds"] == 999
    db.rpc.assert_called_once_with(
        "get_playlist_duration_total", {"p_playlist_id": _PLAYLIST_ID}
    )


def test_get_playlist_duration_total_is_not_limited_to_the_tracks_read():
    _use_db(
        _fake_detail_db(
            entry_rows=[{"track_id": _TRACK_ONE_ID, "position": 1}],
            track_rows=[_TRACK_ONE],
            total_count=1500,
            duration_total=270000,
        )
    )
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    body = response.json()
    assert body["data"]["total_duration_seconds"] == 270000
    assert len(body["data"]["tracks"]) == 1


def test_get_playlist_reads_a_null_is_public_as_false():
    _use_db(_fake_detail_db(playlist_rows=[{**_PLAYLIST_ROW, "is_public": None}]))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["is_public"] is False


def test_get_playlist_unknown_id_returns_playlist_not_found():
    _use_db(_fake_detail_db(playlist_rows=[]))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_get_playlist_owned_by_another_user_returns_playlist_not_found():
    _use_db(_fake_detail_db(playlist_rows=[_OTHER_PLAYLIST_ROW]))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_get_playlist_malformed_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/not-a-uuid")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_get_playlist_track_missing_from_tracks_table_returns_upstream_error():
    _use_db(
        _fake_detail_db(
            entry_rows=[{"track_id": _TRACK_ONE_ID, "position": 1}],
            track_rows=[],
        )
    )
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_malformed_track_row_returns_upstream_error():
    _use_db(
        _fake_detail_db(
            entry_rows=[{"track_id": _TRACK_ONE_ID, "position": 1}],
            track_rows=[{"id": _TRACK_ONE_ID, "track_id": "t1"}],
        )
    )
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_lookup_failure_returns_upstream_error():
    _use_db(_fake_detail_db(playlist_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_lookup_timeout_returns_upstream_timeout():
    _use_db(_fake_detail_db(playlist_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_playlist_tracks_failure_returns_upstream_error():
    _use_db(_fake_detail_db(entries_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_tracks_timeout_returns_upstream_timeout():
    _use_db(
        _fake_detail_db(
            entry_rows=[{"track_id": _TRACK_ONE_ID, "position": 1}],
            tracks_error=httpx.ReadTimeout("timed out"),
        )
    )
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_playlist_duration_failure_returns_upstream_error():
    _use_db(_fake_detail_db(duration_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_duration_timeout_returns_upstream_timeout():
    _use_db(_fake_detail_db(duration_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_playlist_null_duration_total_returns_upstream_error():
    # duration_total=None would fall through to the fixture's derived
    # default rather than staying null, so the mock is pinned by hand here
    # to exercise a database that genuinely answers with no total.
    db = _fake_detail_db()
    db.rpc.return_value.execute.return_value = MagicMock(data=None)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_boolean_duration_total_returns_upstream_error():
    # duration_total=True would fall through to the fixture's derived
    # default rather than staying a bool, so the mock is pinned by hand
    # here to exercise a database that answers true: in Python bool is a
    # subclass of int, so a naive isinstance(data, int) check would let
    # this pass as 1 instead of raising.
    db = _fake_detail_db()
    db.rpc.return_value.execute.return_value = MagicMock(data=True)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_unauthenticated_get_request_returns_unauthorized():
    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- PATCH /playlists/{playlist_id} -----------------------------------


def test_update_playlist_updates_editable_fields():
    updated = {
        **_PLAYLIST_ROW,
        "title": "Night drive",
        "description": "Slower",
        "is_public": True,
    }
    db = _fake_update_db(get_data=[_PLAYLIST_ROW], update_data=[updated])
    _use_db(db)
    _use_auth()

    response = client.patch(
        f"/playlists/{_PLAYLIST_ID}",
        json={"title": "Night drive", "description": "Slower", "is_public": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == updated
    db.table.return_value.update.assert_called_once_with(
        {"title": "Night drive", "description": "Slower", "is_public": True}
    )


def test_update_playlist_partial_body_only_sends_provided_field():
    db = _fake_update_db(get_data=[_PLAYLIST_ROW], update_data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    client.patch(f"/playlists/{_PLAYLIST_ID}", json={"is_public": True})

    db.table.return_value.update.assert_called_once_with({"is_public": True})


def test_update_playlist_never_sends_updated_at():
    db = _fake_update_db(get_data=[_PLAYLIST_ROW], update_data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": "Night drive"})

    fields = db.table.return_value.update.call_args.args[0]
    assert "updated_at" not in fields


def test_update_playlist_ignores_owner_id_from_body():
    db = _fake_update_db(get_data=[_PLAYLIST_ROW], update_data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    client.patch(
        f"/playlists/{_PLAYLIST_ID}",
        json={"title": "Night drive", "owner_id": _OTHER_USER_ID},
    )

    fields = db.table.return_value.update.call_args.args[0]
    assert "owner_id" not in fields


def test_update_playlist_clears_description_with_explicit_null():
    cleared = {**_PLAYLIST_ROW, "description": None}
    db = _fake_update_db(get_data=[_PLAYLIST_ROW], update_data=[cleared])
    _use_db(db)
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"description": None})

    assert response.status_code == 200
    assert response.json()["data"]["description"] is None
    db.table.return_value.update.assert_called_once_with({"description": None})


def test_update_playlist_empty_body_returns_current_state_without_writing():
    db = _fake_update_db(get_data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == _PLAYLIST_ROW
    db.table.return_value.update.assert_not_called()


def test_update_playlist_null_title_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": None})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_update_playlist_blank_title_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": ""})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_update_playlist_over_long_title_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": "x" * 201})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_update_playlist_unknown_id_returns_playlist_not_found():
    db = _fake_update_db(get_data=[])
    _use_db(db)
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": "Night drive"})

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    db.table.return_value.update.assert_not_called()


def test_update_playlist_owned_by_another_user_returns_playlist_not_found():
    db = _fake_update_db(get_data=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": "Night drive"})

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    db.table.return_value.update.assert_not_called()


def test_update_playlist_deleted_between_read_and_write_returns_playlist_not_found():
    # The legacy backend indexed the update result blindly here: an empty
    # result raised IndexError and surfaced as a 500 instead of a 404.
    _use_db(_fake_update_db(get_data=[_PLAYLIST_ROW], update_data=[]))
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": "Night drive"})

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_update_playlist_malformed_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.patch("/playlists/not-a-uuid", json={"title": "Night drive"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_update_playlist_upstream_failure_returns_upstream_error():
    _use_db(
        _fake_update_db(
            get_data=[_PLAYLIST_ROW],
            update_error=APIError({"message": "connection refused"}),
        )
    )
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": "Night drive"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_update_playlist_upstream_timeout_returns_upstream_timeout():
    _use_db(
        _fake_update_db(
            get_data=[_PLAYLIST_ROW],
            update_error=httpx.ReadTimeout("timed out"),
        )
    )
    _use_auth()

    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": "Night drive"})

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_update_request_returns_unauthorized():
    response = client.patch(f"/playlists/{_PLAYLIST_ID}", json={"title": "x"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- DELETE /playlists/{playlist_id} ----------------------------------


def test_delete_playlist_removes_it():
    _use_db(_fake_delete_db(get_data=[_PLAYLIST_ROW], delete_data=[_PLAYLIST_ROW]))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}


def test_delete_does_not_push_the_permission_rule_into_the_query():
    # can_edit is the single permission check. Narrowing the delete with
    # .eq("owner_id", ...) would restate the rule in the query and would
    # not extend to collaborators.
    db = _fake_delete_db(get_data=[_PLAYLIST_ROW], delete_data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    client.delete(f"/playlists/{_PLAYLIST_ID}")

    db.table.return_value.delete.return_value.eq.assert_called_once_with(
        "id", _PLAYLIST_ID
    )


def test_delete_playlist_unknown_id_returns_playlist_not_found():
    db = _fake_delete_db(get_data=[])
    _use_db(db)
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    db.table.return_value.delete.assert_not_called()


def test_delete_playlist_owned_by_another_user_returns_playlist_not_found():
    db = _fake_delete_db(get_data=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    db.table.return_value.delete.assert_not_called()


def test_delete_playlist_deleted_between_read_and_write_returns_playlist_not_found():
    _use_db(_fake_delete_db(get_data=[_PLAYLIST_ROW], delete_data=[]))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_delete_playlist_malformed_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.delete("/playlists/not-a-uuid")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_delete_playlist_upstream_failure_returns_upstream_error():
    _use_db(
        _fake_delete_db(
            get_data=[_PLAYLIST_ROW],
            delete_error=APIError({"message": "connection refused"}),
        )
    )
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_delete_playlist_upstream_timeout_returns_upstream_timeout():
    _use_db(
        _fake_delete_db(
            get_data=[_PLAYLIST_ROW],
            delete_error=httpx.ReadTimeout("timed out"),
        )
    )
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_delete_request_returns_unauthorized():
    response = client.delete(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- POST /playlists/{playlist_id}/tracks -----------------------------


def test_add_track_returns_the_added_track_at_the_position_the_rpc_assigned():
    _use_db(_fake_add_db(rpc_data={"ok": True, "id": _LINK_ROW_ID, "position": 4}))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {**_TRACK_ONE, "position": 4}


def test_add_track_upserts_the_metadata_on_track_id():
    db = _fake_add_db()
    _use_db(db)
    _use_auth()

    client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    db.tables["tracks"].upsert.assert_called_once_with(
        [_ADD_ONE_BODY], on_conflict="track_id"
    )


def test_add_track_calls_the_rpc_with_the_catalog_uuid_and_the_caller():
    # The uuid, not the provider id: playlist_tracks.track_id references
    # tracks.id. The caller is passed explicitly because auth.uid() is null
    # on the service-role client.
    db = _fake_add_db()
    _use_db(db)
    _use_auth()

    client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    db.rpc.assert_called_once_with(
        "add_playlist_track",
        {
            "p_playlist_id": _PLAYLIST_ID,
            "p_track_id": _TRACK_ONE_ID,
            "p_added_by": _USER_ID,
        },
    )


def test_add_track_returns_the_catalog_uuid_not_the_link_row_id():
    # The RPC's id is the playlist_tracks row. PlaylistTrack.id is the
    # catalog uuid, the one GET /playlists/{id} reports, so the response
    # keeps the id the upsert returned.
    _use_db(_fake_add_db(rpc_data={"ok": True, "id": _LINK_ROW_ID, "position": 1}))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.json()["data"]["id"] == _TRACK_ONE_ID


def test_add_track_leaves_the_position_to_the_database():
    # The RPC assigns it in the same statement as the insert, so the service
    # neither reads the highest position nor writes the link itself.
    db = _fake_add_db()
    _use_db(db)
    _use_auth()

    client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert "playlist_tracks" not in db.tables


def test_add_track_accepts_a_single_row_rpc_result():
    _use_db(
        _fake_add_db(rpc_data=[{"ok": True, "id": _LINK_ROW_ID, "position": 4}]),
    )
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 200
    assert response.json()["data"]["position"] == 4


def test_add_track_already_in_the_playlist_returns_conflict():
    # Reported by the RPC rather than found by a read first: ux_playlist_track
    # on (playlist_id, track_id) is what rejects the duplicate.
    db = _fake_add_db(rpc_data={"ok": False, "error": "track_already_in_playlist"})
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "track_already_in_playlist"}
    assert "playlist_tracks" not in db.tables


def test_add_track_without_duration_returns_invalid_request():
    # duration_seconds is optional on a like but required here: a null one
    # would store a track that GET /playlists/{id} cannot serialize, and
    # nothing fills it in afterwards.
    db = MagicMock()
    _use_db(db)
    _use_auth()

    body = {
        key: value for key, value in _ADD_ONE_BODY.items() if key != "duration_seconds"
    }

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_add_track_with_empty_artists_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks",
        json={**_ADD_ONE_BODY, "artists": []},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_add_track_missing_track_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    body = {key: value for key, value in _ADD_ONE_BODY.items() if key != "track_id"}

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_add_track_to_playlist_owned_by_another_user_returns_playlist_not_found():
    db = _fake_add_db(playlist_rows=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    # The permission check runs before anything is written to the catalog.
    assert "tracks" not in db.tables


def test_add_track_to_unknown_playlist_returns_playlist_not_found():
    db = _fake_add_db(playlist_rows=[])
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_add_track_to_a_playlist_deleted_before_the_write_returns_not_found():
    # The playlist can be deleted between the permission check and the call.
    # The RPC finding no playlist is the same 404 the permission check
    # raises, not an upstream anomaly.
    _use_db(_fake_add_db(rpc_data={"ok": False, "error": "playlist_not_found"}))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_add_track_rejected_by_the_rpc_returns_upstream_error():
    # Any refusal other than the duplicate and the missing playlist is an
    # upstream anomaly, not a domain answer the endpoint has a reason for.
    _use_db(_fake_add_db(rpc_data={"ok": False, "error": "playlist_locked"}))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_add_track_without_a_returned_position_returns_upstream_error():
    _use_db(_fake_add_db(rpc_data={"ok": True, "id": _LINK_ROW_ID}))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_add_track_without_returned_catalog_row_returns_upstream_error():
    _use_db(_fake_add_db(upsert_rows=[]))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_add_track_malformed_playlist_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.post("/playlists/not-a-uuid/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_add_track_upstream_failure_returns_upstream_error():
    _use_db(_fake_add_db(rpc_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_add_track_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_add_db(rpc_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_add_track_request_returns_unauthorized():
    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- POST /playlists/{playlist_id}/tracks/bulk ------------------------


def test_bulk_add_links_the_whole_batch_with_one_call():
    # The batch is one statement now: every track goes out in a single
    # add_playlist_tracks_bulk call, in the order they were sent.
    db = _fake_add_db(upsert_rows=[_TRACK_ONE, _TRACK_TWO], rpc_data=_bulk_rpc(added=2))
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"added": 2, "skipped": 0}}
    assert db.rpc.call_count == 1

    name, args = _bulk_call(db)
    assert name == "add_playlist_tracks_bulk"
    assert args["p_playlist_id"] == _PLAYLIST_ID
    assert args["p_track_ids"] == [_TRACK_ONE_ID, _TRACK_TWO_ID]


def test_bulk_add_takes_the_adding_user_from_the_token():
    db = _fake_add_db(rpc_data=_bulk_rpc(added=1))
    _use_db(db)
    _use_auth()

    client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert _bulk_call(db)[1]["p_added_by"] == _USER_ID


def test_bulk_add_resolves_provider_ids_through_the_catalog_write_alone():
    # The RPC takes catalog uuids and the upsert already returns them, so the
    # endpoint reads nothing before writing: no provider-id lookup, and no
    # read of the existing links either — deciding what to skip is the RPC's
    # job, not a query this service runs first.
    db = _fake_add_db(upsert_rows=[_TRACK_ONE, _TRACK_TWO], rpc_data=_bulk_rpc(added=2))
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.status_code == 200
    assert "playlist_tracks" not in db.tables
    db.tables["tracks"].select.assert_not_called()


def test_bulk_add_counts_a_track_repeated_in_the_batch_as_skipped():
    # The RPC never sees the repeat, so it cannot count it: the batch is
    # deduplicated before the catalog write, which one ON CONFLICT statement
    # requires anyway. t2 is already in the playlist, so the RPC skips it and
    # the repeated t1 is the second skip.
    db = _fake_add_db(
        upsert_rows=[_TRACK_ONE, _TRACK_TWO], rpc_data=_bulk_rpc(added=1, skipped=1)
    )
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.json()["data"] == {"added": 1, "skipped": 2}
    # Neither the catalog write nor the link call sees the repeat.
    assert len(db.tables["tracks"].upsert.call_args.args[0]) == 2
    assert len(_bulk_call(db)[1]["p_track_ids"]) == 2


def test_bulk_add_keeps_the_first_occurrence_so_tracks_follow_the_batch():
    db = _fake_add_db(upsert_rows=[_TRACK_ONE, _TRACK_TWO], rpc_data=_bulk_rpc(added=2))
    _use_db(db)
    _use_auth()

    client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_TWO_BODY, _ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert _bulk_call(db)[1]["p_track_ids"] == [_TRACK_TWO_ID, _TRACK_ONE_ID]


def test_bulk_add_reports_the_tracks_the_rpc_skipped():
    db = _fake_add_db(
        upsert_rows=[_TRACK_ONE, _TRACK_TWO], rpc_data=_bulk_rpc(added=1, skipped=1)
    )
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.json()["data"] == {"added": 1, "skipped": 1}


def test_bulk_add_of_tracks_all_already_in_the_playlist_adds_nothing():
    # Nothing to add is a batch the RPC skips whole, not an error. The
    # catalog write still happens: what is already linked is only known
    # inside the RPC now, so the metadata is refreshed on the way there.
    db = _fake_add_db(
        upsert_rows=[_TRACK_ONE, _TRACK_TWO], rpc_data=_bulk_rpc(added=0, skipped=2)
    )
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"added": 0, "skipped": 2}


def test_bulk_add_to_a_playlist_deleted_before_the_write_returns_not_found():
    # The playlist can be deleted between the permission check and the call.
    # The RPC finding no playlist is the same 404 the permission check
    # raises, not an upstream anomaly.
    db = _fake_add_db(
        upsert_rows=[_TRACK_ONE, _TRACK_TWO],
        rpc_data={"ok": False, "error": "playlist_not_found"},
    )
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_bulk_add_rejected_by_the_rpc_returns_upstream_error():
    # Any refusal other than the missing playlist is an upstream anomaly, not
    # a domain answer the endpoint has a reason for.
    db = _fake_add_db(
        upsert_rows=[_TRACK_ONE, _TRACK_TWO],
        rpc_data={"ok": False, "error": "playlist_locked"},
    )
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_bulk_add_without_returned_counts_returns_upstream_error():
    _use_db(_fake_add_db(rpc_data={"ok": True, "added": 1}))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_bulk_add_sends_a_full_batch_in_one_call():
    # The uuids travel in the RPC's request body rather than an in_ filter in
    # the query string, so a full batch needs no splitting to stay under a
    # URI limit.
    bodies, rows = _bulk_batch(200)
    db = _fake_add_db(upsert_rows=rows, rpc_data=_bulk_rpc(added=200))
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": bodies}
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"added": 200, "skipped": 0}
    assert db.rpc.call_count == 1
    assert len(_bulk_call(db)[1]["p_track_ids"]) == 200


def test_bulk_add_over_the_cap_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    bodies = [{**_ADD_ONE_BODY, "track_id": f"t{index}"} for index in range(201)]

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": bodies}
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()
    db.rpc.assert_not_called()


def test_bulk_add_empty_batch_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": []}
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()
    db.rpc.assert_not_called()


def test_bulk_add_with_an_invalid_track_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    broken = {
        key: value for key, value in _ADD_TWO_BODY.items() if key != "duration_seconds"
    }

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, broken]},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()
    db.rpc.assert_not_called()


def test_bulk_add_to_playlist_owned_by_another_user_returns_playlist_not_found():
    db = _fake_add_db(playlist_rows=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    # The permission check runs before anything is written to the catalog.
    assert "tracks" not in db.tables
    db.rpc.assert_not_called()


def test_bulk_add_upstream_failure_returns_upstream_error():
    _use_db(_fake_add_db(upsert_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_bulk_add_failing_rpc_returns_upstream_error():
    # The RPC has no exception handler on purpose: an unexpected database
    # error escapes it and reaches the client library, and nothing of it
    # reaches the response.
    _use_db(_fake_add_db(rpc_error=APIError({"message": "deadlock detected"})))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_bulk_add_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_add_db(upsert_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_bulk_add_timing_out_rpc_returns_upstream_timeout():
    _use_db(_fake_add_db(rpc_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_bulk_add_request_returns_unauthorized():
    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- DELETE /playlists/{playlist_id}/tracks/{track_id} ----------------


def test_remove_track_unlinks_through_the_rpc():
    # The whole removal is the RPC: it takes the playlist row lock, resolves
    # the provider id and deletes, so the service writes nothing itself.
    db = _fake_remove_db()
    _use_db(db)
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    db.rpc.assert_called_once_with(
        "remove_playlist_track",
        {"p_playlist_id": _PLAYLIST_ID, "p_track_id": "t1"},
    )


def test_remove_track_passes_the_provider_id_untouched():
    # The path carries the provider id, the one a client holds during
    # playback. playlist_tracks joins on the catalog uuid, but resolving the
    # two is the RPC's job now, so nothing looks it up here first.
    db = _fake_remove_db()
    _use_db(db)
    _use_auth()

    client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert db.rpc.call_args.args[1]["p_track_id"] == "t1"
    assert "tracks" not in db.tables
    assert "playlist_tracks" not in db.tables


@pytest.mark.parametrize(
    "track_id",
    [
        # A track that is simply not in the playlist, and one the catalog has
        # never seen. The second resolves to nothing inside the RPC, which is
        # the same deleted:0 as the first, so both take this one path.
        "t1",
        "nope",
    ],
)
def test_remove_track_with_nothing_to_delete_is_not_an_error(track_id):
    # Nothing deleted is the state the caller asked for. Removing is
    # idempotent, so this is a success, not a 404.
    _use_db(_fake_remove_db(rpc_data={"ok": True, "deleted": 0}))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/{track_id}")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}


def test_remove_track_from_a_playlist_deleted_before_the_write_returns_not_found():
    # The playlist can be deleted between the permission check and the call,
    # the same race the bulk add has.
    _use_db(_fake_remove_db(rpc_data={"ok": False, "error": "playlist_not_found"}))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_remove_track_rejected_by_the_rpc_returns_upstream_error():
    _use_db(_fake_remove_db(rpc_data={"ok": False, "error": "playlist_locked"}))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_remove_track_without_a_returned_count_returns_upstream_error():
    _use_db(_fake_remove_db(rpc_data={"ok": True}))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_remove_track_from_playlist_owned_by_another_user_returns_not_found():
    db = _fake_remove_db(playlist_rows=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    # can_edit still decides before anything is removed: the RPC checks that
    # the playlist exists, not who may write to it.
    db.rpc.assert_not_called()


def test_remove_track_malformed_playlist_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.delete("/playlists/not-a-uuid/tracks/t1")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()
    db.rpc.assert_not_called()


def test_remove_track_upstream_failure_returns_upstream_error():
    _use_db(_fake_remove_db(rpc_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_remove_track_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_remove_db(rpc_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_remove_track_request_returns_unauthorized():
    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- POST /playlists/{playlist_id}/move-track -------------------------


def test_move_track_calls_the_rpc_with_one_based_indices():
    db = _fake_move_db(track_count=3)
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 3},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    db.rpc.assert_called_once_with(
        "move_playlist_track",
        {
            "p_playlist_id": _PLAYLIST_ID,
            "p_old_index": 1,
            "p_new_index": 3,
        },
    )


def test_move_track_to_the_last_position_is_allowed():
    db = _fake_move_db(track_count=2)
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 200


def test_move_track_past_the_end_returns_invalid_request():
    # The RPC clamps an out-of-range index to the nearest valid one, so
    # passing it through would report a move that did something else.
    db = _fake_move_db(track_count=2)
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 5},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.rpc.assert_not_called()


def test_move_track_from_past_the_end_returns_invalid_request():
    db = _fake_move_db(track_count=2)
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 7, "new_position": 1},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.rpc.assert_not_called()


def test_move_track_with_a_zero_position_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 0, "new_position": 1},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_move_track_in_an_empty_playlist_returns_invalid_request():
    db = _fake_move_db(track_count=0)
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 1},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.rpc.assert_not_called()


def test_move_track_rejected_by_the_rpc_returns_upstream_error():
    # The RPC reports failure in its payload, not as an error status, so a
    # successful round trip can still mean it refused.
    _use_db(_fake_move_db(rpc_data={"ok": False, "error": "playlist_locked"}))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_move_track_accepts_a_single_row_rpc_result():
    # A set-returning function hands the object back wrapped in a list.
    _use_db(_fake_move_db(rpc_data=[{"ok": True, "order": [_TRACK_ONE_ID]}]))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 200


def test_move_track_in_playlist_owned_by_another_user_returns_not_found():
    db = _fake_move_db(playlist_rows=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    db.rpc.assert_not_called()


def test_move_track_malformed_playlist_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.post(
        "/playlists/not-a-uuid/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_move_track_upstream_failure_returns_upstream_error():
    _use_db(_fake_move_db(rpc_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_move_track_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_move_db(rpc_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_move_track_request_returns_unauthorized():
    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- GET /playlists/owned-with-track/{track_id} -----------------------


def test_owned_playlists_with_track_returns_the_playlist_ids():
    _use_db(_fake_rpc_db(data=[{"id": _PLAYLIST_ID}]))
    _use_auth()

    response = client.get("/playlists/owned-with-track/t1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"playlist_ids": [_PLAYLIST_ID]}}


def test_owned_playlists_with_track_passes_the_user_id_from_the_token():
    db = _fake_rpc_db(data=[])
    _use_db(db)
    _use_auth(user_id=_OTHER_USER_ID)

    client.get("/playlists/owned-with-track/t1")

    db.rpc.assert_called_once_with(
        "get_owned_playlists_with_track",
        {"p_user_id": _OTHER_USER_ID, "p_track_id": "t1"},
    )


def test_owned_playlists_with_track_accepts_bare_ids():
    # A function returning setof uuid hands back the ids themselves rather
    # than single-column rows.
    _use_db(_fake_rpc_db(data=[_PLAYLIST_ID]))
    _use_auth()

    response = client.get("/playlists/owned-with-track/t1")

    assert response.json()["data"] == {"playlist_ids": [_PLAYLIST_ID]}


def test_owned_playlists_with_track_no_match_returns_an_empty_list():
    # A membership question with the answer "none" is ok:true, not an
    # empty state: nothing is missing, the answer is simply no.
    _use_db(_fake_rpc_db(data=[]))
    _use_auth()

    response = client.get("/playlists/owned-with-track/t1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"playlist_ids": []}}


def test_owned_playlists_with_track_upstream_failure_returns_upstream_error():
    _use_db(_fake_rpc_db(error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.get("/playlists/owned-with-track/t1")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_owned_playlists_with_track_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_rpc_db(error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/playlists/owned-with-track/t1")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_owned_playlists_with_track_returns_unauthorized():
    response = client.get("/playlists/owned-with-track/t1")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
