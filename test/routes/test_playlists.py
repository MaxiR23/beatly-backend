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
# - A malformed playlist id is 422 invalid_request, without reaching the
#   database
# - Every endpoint returns 401 without an Authorization header and
#   502/504 when the database fails or times out
#
# What is covered:
# - Happy path, expected empty state, partial update, invalid input,
#   playlist not found on read, update and delete, permission scoping,
#   upstream failure, upstream timeout, unauthenticated access
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
