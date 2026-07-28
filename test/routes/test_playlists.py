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
# - GET /playlists lists the caller's playlists, newest first
# - Returns 200 with ok:false and reason "no_playlists" when the caller
#   has no playlists
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
#   and links it at the next free position
# - The first track of an empty playlist lands at position 1
# - Adding a track already in the playlist is 409
#   track_already_in_playlist and writes no link
# - POST /playlists/{id}/tracks rejects a missing duration_seconds or an
#   empty artists list with 422, without reaching the database
# - POST /playlists/{id}/tracks/bulk adds tracks at consecutive
#   positions and reports added and skipped
# - A track repeated inside one batch is added once and counted as
#   skipped for the rest
# - Tracks already in the playlist are skipped, and a batch with nothing
#   left to add writes nothing
# - The existing-link check is batched, so a full batch does not build a
#   URI the database rejects
# - An empty batch and one over 200 tracks are both 422, without
#   reaching the database
# - DELETE /playlists/{id}/tracks/{track_id} resolves the provider id to
#   the catalog uuid and removes the link
# - Removing a track that is not in the playlist, or one the catalog
#   does not know, is 200 rather than an error
# - POST /playlists/{id}/move-track calls the move_playlist_track RPC
#   with 1-based indices
# - A position of zero or past the end of the playlist is 422 and the
#   RPC is never called, so the RPC's clamp is never relied on
# - An RPC answering ok:false is 502, not a silent success
# - GET /playlists/owned-with-track/{track_id} returns the caller's
#   playlist ids for a provider id, passing the user id from the token
# - A track in none of the caller's playlists is ok:true with an empty
#   list, not an empty state
# - Every track endpoint refuses a playlist the caller cannot edit with
#   404 playlist_not_found, before touching any other table
# - A malformed playlist id is 422 invalid_request, without reaching the
#   database
# - Every endpoint returns 401 without an Authorization header and
#   502/504 when the database fails or times out
#
# What is covered:
# - Happy path, expected empty state, partial update, invalid input,
#   playlist not found on read, update and delete, permission scoping,
#   duplicate conflict, idempotent removal, batch deduplication and
#   skipping, position validation, upstream failure, upstream timeout,
#   unauthenticated access
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

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
_PLAYLIST_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

_TRACK_ONE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_TRACK_TWO_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

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


def _fake_list_db(data=None, error=None):
    db = MagicMock()
    query = db.table.return_value.select.return_value.eq.return_value.order.return_value
    if error is not None:
        query.execute.side_effect = error
    else:
        query.execute.return_value = MagicMock(data=data)
    return db


def _fake_detail_db(
    playlist_rows=None,
    entry_rows=None,
    track_rows=None,
    total_count=None,
    playlist_error=None,
    entries_error=None,
    tracks_error=None,
):
    if playlist_rows is None:
        playlist_rows = [_PLAYLIST_ROW]
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
            query = table_mock.select.return_value.eq.return_value
            if playlist_error is not None:
                query.execute.side_effect = playlist_error
            else:
                query.execute.return_value = MagicMock(data=playlist_rows)
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
    return db


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
    linked_rows=None,
    max_position=None,
    insert_rows=None,
    playlist_error=None,
    upsert_error=None,
    linked_error=None,
    insert_error=None,
):
    if playlist_rows is None:
        playlist_rows = [_PLAYLIST_ROW]
    if upsert_rows is None:
        upsert_rows = [_TRACK_ONE]
    if insert_rows is None:
        insert_rows = [{"playlist_id": _PLAYLIST_ID}]

    def configure(name, table):
        if name == "playlists":
            _pin(
                table.select.return_value.eq.return_value,
                data=playlist_rows,
                error=playlist_error,
            )
        elif name == "tracks":
            _pin(table.upsert.return_value, data=upsert_rows, error=upsert_error)
        elif name == "playlist_tracks":
            scoped = table.select.return_value.eq.return_value
            _pin(scoped.in_.return_value, data=linked_rows, error=linked_error)
            _pin(
                scoped.order.return_value.limit.return_value,
                data=[] if max_position is None else [{"position": max_position}],
            )
            _pin(table.insert.return_value, data=insert_rows, error=insert_error)

    return _fake_multi_table_db(configure)


def _fake_remove_db(
    playlist_rows=None,
    lookup_rows=None,
    delete_rows=None,
    playlist_error=None,
    lookup_error=None,
    delete_error=None,
):
    if playlist_rows is None:
        playlist_rows = [_PLAYLIST_ROW]
    if lookup_rows is None:
        lookup_rows = [{"id": _TRACK_ONE_ID}]

    def configure(name, table):
        if name == "playlists":
            _pin(
                table.select.return_value.eq.return_value,
                data=playlist_rows,
                error=playlist_error,
            )
        elif name == "tracks":
            _pin(
                table.select.return_value.eq.return_value,
                data=lookup_rows,
                error=lookup_error,
            )
        elif name == "playlist_tracks":
            _pin(
                table.delete.return_value.eq.return_value.eq.return_value,
                data=delete_rows,
                error=delete_error,
            )

    return _fake_multi_table_db(configure)


def _fake_move_db(
    playlist_rows=None,
    track_count=2,
    rpc_data=None,
    playlist_error=None,
    count_error=None,
    rpc_error=None,
):
    if playlist_rows is None:
        playlist_rows = [_PLAYLIST_ROW]
    if rpc_data is None:
        rpc_data = {"ok": True, "order": [_TRACK_TWO_ID, _TRACK_ONE_ID]}

    def configure(name, table):
        if name == "playlists":
            _pin(
                table.select.return_value.eq.return_value,
                data=playlist_rows,
                error=playlist_error,
            )
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


def test_returns_playlists_newest_first():
    db = _fake_list_db(data=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.get("/playlists")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"playlists": [_PLAYLIST_ROW]}
    query = db.table.return_value.select.return_value.eq.return_value
    query.order.assert_called_once_with("created_at", desc=True)


def test_no_playlists_returns_no_playlists():
    _use_db(_fake_list_db(data=[]))
    _use_auth()

    response = client.get("/playlists")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_playlists"}


def test_list_reads_a_null_is_public_as_false():
    _use_db(_fake_list_db(data=[{**_PLAYLIST_ROW, "is_public": None}]))
    _use_auth()

    response = client.get("/playlists")

    assert response.status_code == 200
    assert response.json()["data"]["playlists"][0]["is_public"] is False


def test_list_scopes_query_to_authenticated_user():
    db = _fake_list_db(data=[])
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
    entry_rows = [
        {"track_id": f"{index:08d}-0000-0000-0000-000000000000", "position": index}
        for index in range(200)
    ]
    track_rows = [
        {**_TRACK_ONE, "id": row["track_id"], "track_id": f"t{row['position']}"}
        for row in entry_rows
    ]
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
    entry_rows = [
        {"track_id": f"{index:08d}-0000-0000-0000-000000000000", "position": index}
        for index in range(200)
    ]
    track_rows = [
        {**_TRACK_ONE, "id": row["track_id"], "track_id": f"t{row['position']}"}
        for row in entry_rows
    ]
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
    _use_db(_fake_detail_db(entry_rows=[]))
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["tracks"] == []
    assert body["data"]["total_count"] == 0
    assert body["data"]["has_more"] is False


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


def test_add_track_returns_the_added_track_at_the_next_position():
    _use_db(_fake_add_db(max_position=3))
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


def test_add_track_links_the_catalog_uuid_not_the_provider_id():
    db = _fake_add_db(max_position=3)
    _use_db(db)
    _use_auth()

    client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    db.tables["playlist_tracks"].insert.assert_called_once_with(
        {
            "playlist_id": _PLAYLIST_ID,
            "track_id": _TRACK_ONE_ID,
            "position": 4,
        }
    )


def test_add_track_to_an_empty_playlist_starts_at_position_one():
    db = _fake_add_db(max_position=None)
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.json()["data"]["position"] == 1


def test_add_track_reuses_a_gap_free_position_above_the_highest():
    # Positions are taken from the highest one, not the track count: a
    # removed track leaves its position free and ux_playlist_pos on
    # (playlist_id, position) would reject reusing it.
    db = _fake_add_db(max_position=9)
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.json()["data"]["position"] == 10


def test_add_track_already_in_the_playlist_returns_conflict():
    db = _fake_add_db(linked_rows=[{"track_id": _TRACK_ONE_ID}])
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "track_already_in_playlist"}
    db.tables["playlist_tracks"].insert.assert_not_called()


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


def test_add_track_without_returned_link_returns_upstream_error():
    _use_db(_fake_add_db(insert_rows=[]))
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
    _use_db(_fake_add_db(insert_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_add_track_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_add_db(insert_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_add_track_request_returns_unauthorized():
    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- POST /playlists/{playlist_id}/tracks/bulk ------------------------


def test_bulk_add_adds_tracks_at_consecutive_positions():
    db = _fake_add_db(upsert_rows=[_TRACK_ONE, _TRACK_TWO], max_position=5)
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"added": 2, "skipped": 0}}
    db.tables["playlist_tracks"].insert.assert_called_once_with(
        [
            {"playlist_id": _PLAYLIST_ID, "track_id": _TRACK_ONE_ID, "position": 6},
            {"playlist_id": _PLAYLIST_ID, "track_id": _TRACK_TWO_ID, "position": 7},
        ]
    )


def test_bulk_add_counts_a_track_repeated_in_the_batch_once():
    db = _fake_add_db(upsert_rows=[_TRACK_ONE, _TRACK_TWO])
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.json()["data"] == {"added": 2, "skipped": 1}
    # The repeat never reaches the catalog write either.
    assert len(db.tables["tracks"].upsert.call_args.args[0]) == 2


def test_bulk_add_keeps_the_first_occurrence_so_positions_follow_the_batch():
    db = _fake_add_db(upsert_rows=[_TRACK_ONE, _TRACK_TWO])
    _use_db(db)
    _use_auth()

    client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_TWO_BODY, _ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    written = db.tables["playlist_tracks"].insert.call_args.args[0]
    assert [row["track_id"] for row in written] == [_TRACK_TWO_ID, _TRACK_ONE_ID]


def test_bulk_add_skips_tracks_already_in_the_playlist():
    db = _fake_add_db(
        upsert_rows=[_TRACK_ONE, _TRACK_TWO],
        linked_rows=[{"track_id": _TRACK_ONE_ID}],
    )
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.json()["data"] == {"added": 1, "skipped": 1}
    written = db.tables["playlist_tracks"].insert.call_args.args[0]
    assert [row["track_id"] for row in written] == [_TRACK_TWO_ID]


def test_bulk_add_with_nothing_left_to_add_writes_nothing():
    db = _fake_add_db(
        upsert_rows=[_TRACK_ONE, _TRACK_TWO],
        linked_rows=[{"track_id": _TRACK_ONE_ID}, {"track_id": _TRACK_TWO_ID}],
    )
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.json()["data"] == {"added": 0, "skipped": 2}
    db.tables["playlist_tracks"].insert.assert_not_called()


def test_bulk_add_checks_existing_links_in_batches():
    # The existing-link check filters on in_, which goes into the query
    # string, so a full batch in one filter builds a URI Supabase rejects.
    bodies = [{**_ADD_ONE_BODY, "track_id": f"t{index}"} for index in range(200)]
    upsert_rows = [
        {
            **_TRACK_ONE,
            "id": f"{index:08d}-0000-0000-0000-000000000000",
            "track_id": f"t{index}",
        }
        for index in range(200)
    ]
    db = _fake_add_db(upsert_rows=upsert_rows)
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": bodies}
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"added": 200, "skipped": 0}
    in_mock = db.tables["playlist_tracks"].select.return_value.eq.return_value.in_
    assert in_mock.call_count == 2
    assert len(in_mock.call_args_list[0].args[1]) == 150
    assert len(in_mock.call_args_list[1].args[1]) == 50


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


def test_bulk_add_to_playlist_owned_by_another_user_returns_playlist_not_found():
    db = _fake_add_db(playlist_rows=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    assert "tracks" not in db.tables


def test_bulk_add_upstream_failure_returns_upstream_error():
    _use_db(_fake_add_db(upsert_error=APIError({"message": "connection refused"})))
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


def test_unauthenticated_bulk_add_request_returns_unauthorized():
    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- DELETE /playlists/{playlist_id}/tracks/{track_id} ----------------


def test_remove_track_deletes_the_link():
    db = _fake_remove_db(delete_rows=[{"track_id": _TRACK_ONE_ID}])
    _use_db(db)
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}


def test_remove_track_resolves_the_provider_id_to_the_catalog_uuid():
    # The path carries the provider id, the one a client holds during
    # playback; playlist_tracks joins on the catalog uuid.
    db = _fake_remove_db()
    _use_db(db)
    _use_auth()

    client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    db.tables["tracks"].select.return_value.eq.assert_called_once_with("track_id", "t1")
    scoped = db.tables["playlist_tracks"].delete.return_value.eq
    scoped.assert_called_once_with("playlist_id", _PLAYLIST_ID)
    scoped.return_value.eq.assert_called_once_with("track_id", _TRACK_ONE_ID)


def test_remove_track_not_in_the_playlist_is_not_an_error():
    _use_db(_fake_remove_db(delete_rows=[]))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}


def test_remove_track_unknown_to_the_catalog_is_not_an_error():
    db = _fake_remove_db(lookup_rows=[])
    _use_db(db)
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/nope")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    # Nothing to unlink, so the link table is never touched.
    assert "playlist_tracks" not in db.tables


def test_remove_track_from_playlist_owned_by_another_user_returns_not_found():
    db = _fake_remove_db(playlist_rows=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    assert "playlist_tracks" not in db.tables


def test_remove_track_malformed_playlist_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.delete("/playlists/not-a-uuid/tracks/t1")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_remove_track_upstream_failure_returns_upstream_error():
    _use_db(_fake_remove_db(delete_error=APIError({"message": "connection refused"})))
    _use_auth()

    response = client.delete(f"/playlists/{_PLAYLIST_ID}/tracks/t1")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_remove_track_upstream_timeout_returns_upstream_timeout():
    _use_db(_fake_remove_db(delete_error=httpx.ReadTimeout("timed out")))
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
