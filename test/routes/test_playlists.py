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
# - GET /playlists/{id} returns metadata and aggregates only, since #139:
#   no tracks, no has_more — total_count comes from a count="exact"
#   probe on playlist_tracks, never a track row read
# - An empty playlist reports total_count: 0 and total_duration_seconds:
#   0, not null, and never touches the tracks table
# - GET /playlists/{id} returns total_duration_seconds calculated by the
#   database, not summed in Python
# - A failure, a timeout, or a non-numeric payload from the duration RPC
#   are 502/504, the same as any other upstream failure on this endpoint
# - A boolean payload from the duration RPC is 502, not 200 with
#   total_duration_seconds: 1 — bool is a subclass of int in Python
# - A failure or a timeout on the track-count probe is 502/504
# - GET /playlists/{id} returns 404 playlist_not_found for an unknown
#   playlist and for one owned by another user
# - GET /playlists/{id}/tracks (new) returns a cursor-paginated
#   data.items + data.page: order_key ascending with playlist_tracks.id
#   breaking ties, items without the catalog uuid (P1=B), and position
#   the 1-based index across the whole playlist (global, not per page)
# - A cursored page's position continues from a preceding-rows count
#   (through_cursor_filter), queried only when a cursor is present — the
#   first page never pays for it
# - A missing or non-int preceding count is 502, not position starting
#   from 1 again
# - has_more/next_cursor derive from the limit+1 probe row; the last
#   page has no next_cursor
# - A recorrido with limit 1 or 2 across several pages reconstructs the
#   whole playlist once each, with consecutive global positions
# - A track moved behind an in-flight cursor is skipped exactly once on
#   the walk that follows; one moved ahead of it is seen twice — both
#   documented as expected, the cursor itself staying valid throughout
# - Malformed playlist_id and an invalid limit or cursor (including one
#   from GET /playlists, a different sort key) are 422, before touching
#   the database
# - GET /playlists/liked/tracks (new) is the same shape, ordered by
#   user_likes.created_at ascending with track_id breaking ties — the
#   same order GET /likes uses, so a GET /likes cursor is valid here too
# - GET /playlists/liked/tracks has no parent to be missing: for an
#   authenticated user this playlist always exists
# - GET /playlists/liked/tracks is matched before
#   GET /playlists/{playlist_id}/tracks: it never reads the playlists
#   table
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
#   and links it through the add_playlist_track RPC, which computes the
#   position under its own lock (#137); the service reads playlist_tracks
#   only for the last order_key, computed with fractional_indexing and
#   sent to the RPC
# - The response carries the catalog uuid, not the playlist_tracks row id
#   the RPC returns
# - An RPC answering track_already_in_playlist is 409, a playlist deleted
#   between the permission check and the write is 404, and any other
#   refusal is 502
# - An RPC answering order_key_conflict retries with a freshly read key,
#   up to 3 attempts in total, and gives up with 409 order_key_conflict
#   if every attempt collides
# - POST /playlists/{id}/tracks rejects a missing duration_seconds or an
#   empty artists list with 422, without reaching the database
# - POST /playlists/{id}/tracks/bulk links the whole batch with one
#   add_playlist_tracks_bulk call, in the order the tracks were sent, and
#   reports added and skipped
# - The adding user comes from the token
# - One order_key per unique track is generated after the playlist's last
#   one, in the same order as the batch, and sent alongside the track ids;
#   the catalog upsert and the last-key read are the only reads before the
#   write, since what is skipped is the RPC's decision
# - A track repeated inside one batch is added once and counted as
#   skipped for the rest, and reaches neither the catalog write nor the
#   RPC a second time
# - Tracks the RPC skipped are reported as skipped, and a batch it skips
#   whole is added:0 rather than an error
# - A playlist deleted between the permission check and the write is 404,
#   and any other refusal by the RPC is 502
# - The same order_key_conflict retry and 409-when-exhausted behaviour as
#   the single add, generating a fresh set of keys each attempt
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
#   with 1-based indices and an order_key read from up to 3 neighbouring
#   rows, ordered by order_key, and computed with fractional_indexing
# - A position of zero or past the end of the playlist is 422 and the
#   RPC is never called, so the RPC's clamp is never relied on, and the
#   neighbours are never read either
# - An RPC answering ok:false is 502, not a silent success
# - An RPC answering order_key_conflict retries with a freshly read
#   window and a freshly computed key, up to 3 attempts in total, and
#   gives up with 409 order_key_conflict if every attempt collides
# - GET /playlists/owned-with-track/{track_id} returns the caller's
#   playlist ids for a provider id, passing the user id from the token
# - A track in none of the caller's playlists is ok:true with an empty
#   list, not an empty state
# - GET /playlists/liked returns a PlaylistDetail-shaped virtual playlist
#   since #139: metadata and aggregates only, no tracks, with id and
#   title both the literal "liked"
# - total_count and the oldest active like's created_at come from one
#   summary query, ascending, count="exact" + limit(1) — no track row
#   read at all
# - updated_at is the created_at of the most recent active like, from its
#   own query ordered created_at descending, skipped entirely when the
#   summary is empty
# - A user with no active likes gets ok:true with total_count: 0,
#   total_duration_seconds: 0, and created_at == updated_at
# - total_duration_seconds comes from get_liked_tracks_duration_total, not
#   summed in Python
# - A failure or a timeout on the summary, the most-recent-like probe, or
#   the duration RPC is 502/504
# - A non-numeric duration RPC payload is 502
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
#   upstream timeout, unauthenticated access, order_key generation and
#   retry after a collision reported by the RPC
#
# get_user_playlist_thumbnails ordering by order_key is not covered here:
# the ORDER BY lives inside the RPC's SQL body, and the mocked database
# only sees this RPC's name and arguments, neither of which changes. See
# db/migrations/README.md (028 entry) and the QA checklist there instead.
#
# Also not covered: whether the two RPCs' own SQL (the single UPDATE
# move_playlist_track now makes, and the COUNT(*) add_playlist_track
# takes under its lock, both added by #137) actually behaves this way
# against a real Postgres -- the mocked database only sees the RPC's name,
# arguments and the JSON it is told to answer with. See
# db/migrations/README.md (029 entry) for that QA.
#
# Not covered (#139): the real cost of the preceding-rows count on
# GET /playlists/{id}/tracks and GET /playlists/liked/tracks -- that
# depends on Postgres's query plan over real data, which the mocked
# database has no plan at all for; see docs/adr/README.md's correction to
# 009 and the Paso 0 measurement it references. Also not covered: the
# preceding count and the page read are two separate, non-transactional
# requests against the real database, so a concurrent write landing
# between them can shift a page's position by one -- accepted (P3), and
# the mock cannot exercise two requests racing each other at all.
#
# The two catalog-batching tests under "GET /public/playlists/{playlist_id}"
# below moved here from GET /playlists/{playlist_id} (#139): with
# MAX_LIMIT capping a page's fetch at 101 rows, under _TRACK_BATCH_SIZE
# (150), neither paginated endpoint can reach a batched catalog read
# anymore, and test/routes/test_public.py is frozen for this issue.
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
    Cursor,
    SortKey,
    ValueType,
    decode_cursor,
    encode_cursor,
    keyset_filter,
    through_cursor_filter,
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

# Mirrors _TRACKS_SORT and _LIKED_SORT in services/playlist_service.py
# (#139), for the same reason as _LIST_SORT above.
_TRACKS_SORT = SortKey("order_key", ValueType.TEXT, descending=False)
_LIKED_TRACKS_SORT = SortKey(
    "created_at",
    ValueType.TIMESTAMP,
    descending=False,
    id_column="track_id",
    id_type=ValueType.TEXT,
)

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
    rpc_responses=None,
    playlist_error=None,
    upsert_error=None,
    rpc_error=None,
    last_key_rows=None,
    last_key_error=None,
):
    # Both add endpoints touch the same two tables, one RPC and (new for
    # order_key) a read of the playlist's last order_key. rpc_data is the
    # payload the RPC answers with, in the shape of whichever of the two
    # the test is exercising. rpc_responses, when given, is a sequence of
    # payloads for successive db.rpc(...).execute() calls -- a retry after
    # order_key_conflict -- and rpc_data is ignored when it is set.
    # last_key_rows defaults to [], an empty playlist with no last row.
    if upsert_rows is None:
        upsert_rows = [_TRACK_ONE]
    if rpc_data is None:
        rpc_data = {"ok": True, "id": _LINK_ROW_ID, "position": 1}
    if last_key_rows is None:
        last_key_rows = []

    def configure(name, table):
        if name == "playlists":
            _pin_playlist(table, playlist_rows, playlist_error)
        elif name == "tracks":
            _pin(table.upsert.return_value, data=upsert_rows, error=upsert_error)
        elif name == "playlist_tracks":
            _pin(
                table.select.return_value.eq.return_value.order.return_value.limit.return_value,
                data=last_key_rows,
                error=last_key_error,
            )

    db = _fake_multi_table_db(configure)
    if rpc_responses is not None:
        db.rpc.return_value.execute.side_effect = [
            MagicMock(data=data) for data in rpc_responses
        ]
    else:
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
    window_rows=None,
    rpc_data=None,
    rpc_responses=None,
    playlist_error=None,
    count_error=None,
    window_error=None,
    rpc_error=None,
):
    # window_rows is what .range() returns for the neighbour read; the
    # default, two rows, is what a 2-track playlist's 1 -> 2 move (most of
    # the tests below) reads for range(0, 2). rpc_responses, when given, is
    # a sequence of payloads for successive db.rpc(...).execute() calls --
    # a retry after order_key_conflict -- and rpc_data is ignored then.
    if rpc_data is None:
        rpc_data = {"ok": True, "order": [_TRACK_TWO_ID, _TRACK_ONE_ID]}
    if window_rows is None:
        window_rows = [{"order_key": "a0"}, {"order_key": "a1"}]

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
            _pin(
                table.select.return_value.eq.return_value.order.return_value.range.return_value,
                data=window_rows,
                error=window_error,
            )

    db = _fake_multi_table_db(configure)
    if rpc_responses is not None:
        db.rpc.return_value.execute.side_effect = [
            MagicMock(data=data) for data in rpc_responses
        ]
    else:
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

# Since #139 this endpoint no longer reads or returns tracks: it is
# metadata plus aggregates, exactly like GET /playlists/{playlist_id}.
# See "GET /playlists/liked/tracks" below for the paginated track list.

# Two active likes, oldest first -- the order GET /playlists/liked/tracks
# returns tracks in. Distinct timestamps so created_at/updated_at
# derivation is observable.
_LIKE_ONE_ROW = {
    "track_id": _TRACK_ONE["track_id"],
    "created_at": "2026-01-01T00:00:00Z",
}
_LIKE_TWO_ROW = {
    "track_id": _TRACK_TWO["track_id"],
    "created_at": "2026-01-02T00:00:00Z",
}


def _fake_liked_detail_db(
    total_count=None,
    oldest_created_at=None,
    latest_created_at=None,
    duration_total=None,
    summary_error=None,
    latest_error=None,
    duration_error=None,
):
    if total_count is None:
        total_count = 0
    if duration_total is None:
        duration_total = 0

    db = MagicMock()
    # Memoized so a test can assert against the same table mock the
    # request used, via db.tables["user_likes"].
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]

        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "user_likes":
            base = _chain(table_mock, "select", "eq", "is_")
            # _liked_summary and _latest_liked_created_at share the exact
            # same select().eq().is_().order().limit() shape -- the only
            # difference is order()'s desc kwarg, which a MagicMock chain
            # cannot distinguish by depth alone (both are one order() call
            # then limit()). Dispatched explicitly here, the same problem
            # the old two-order-call entries read never had.
            asc_node = MagicMock()
            desc_node = MagicMock()
            base.order.side_effect = lambda column, desc=False: (
                desc_node if desc else asc_node
            )

            summary_leaf = asc_node.limit.return_value
            if summary_error is not None:
                summary_leaf.execute.side_effect = summary_error
            else:
                summary_data = (
                    []
                    if oldest_created_at is None
                    else [{"created_at": oldest_created_at}]
                )
                summary_leaf.execute.return_value = MagicMock(
                    data=summary_data, count=total_count
                )

            latest_leaf = desc_node.limit.return_value
            if latest_error is not None:
                latest_leaf.execute.side_effect = latest_error
            else:
                latest_data = (
                    []
                    if latest_created_at is None
                    else [{"created_at": latest_created_at}]
                )
                latest_leaf.execute.return_value = MagicMock(data=latest_data)
            # Exposed so a test can assert the probe was (not) reached
            # without recomputing the side_effect dispatch itself.
            table_mock.latest_leaf = latest_leaf
        return table_mock

    db.table.side_effect = table_side_effect
    db.tables = tables
    _pin(db.rpc.return_value, data=duration_total, error=duration_error)
    return db


def test_get_liked_playlist_returns_metadata_and_aggregates_without_tracks():
    db = _fake_liked_detail_db(
        total_count=2,
        oldest_created_at=_LIKE_ONE_ROW["created_at"],
        latest_created_at=_LIKE_TWO_ROW["created_at"],
        duration_total=420,
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert set(data.keys()) == {
        "id",
        "owner_id",
        "title",
        "description",
        "is_public",
        "created_at",
        "updated_at",
        "total_count",
        "total_duration_seconds",
    }
    assert data["id"] == "liked"
    assert data["title"] == "liked"
    assert data["owner_id"] == _USER_ID
    assert data["is_public"] is False
    assert data["description"] is None
    assert data["created_at"] == _LIKE_ONE_ROW["created_at"]
    assert data["updated_at"] == _LIKE_TWO_ROW["created_at"]
    assert data["total_count"] == 2
    assert data["total_duration_seconds"] == 420


def test_get_liked_playlist_is_scoped_and_ordered_ascending():
    db = _fake_liked_detail_db(
        total_count=1, oldest_created_at=_LIKE_ONE_ROW["created_at"]
    )
    _use_db(db)
    _use_auth()

    client.get("/playlists/liked")

    likes_table = db.tables["user_likes"]
    likes_table.select.return_value.eq.assert_any_call("user_id", _USER_ID)
    likes_table.select.return_value.eq.return_value.is_.assert_any_call(
        "deleted_at", "null"
    )
    base = _chain(likes_table, "select", "eq", "is_")
    base.order.assert_any_call("created_at")
    base.order.assert_any_call("created_at", desc=True)


def test_get_liked_playlist_with_no_likes_returns_zero_totals():
    db = _fake_liked_detail_db(total_count=0, duration_total=0)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["total_count"] == 0
    assert data["total_duration_seconds"] == 0
    assert data["created_at"] == data["updated_at"]

    # The most-recent-like probe is skipped entirely: an empty summary
    # already answers that there is no most recent like either.
    likes_table = db.tables["user_likes"]
    likes_table.latest_leaf.execute.assert_not_called()


def test_get_liked_playlist_updated_at_is_the_most_recent_active_like():
    # oldest_created_at is old; the descending probe answers a newer one.
    # updated_at must be the probe's answer, created_at the oldest.
    db = _fake_liked_detail_db(
        total_count=2,
        oldest_created_at=_LIKE_ONE_ROW["created_at"],
        latest_created_at="2027-01-01T00:00:00Z",
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    data = response.json()["data"]
    assert data["created_at"] == _LIKE_ONE_ROW["created_at"]
    assert data["updated_at"] == "2027-01-01T00:00:00Z"


def test_get_liked_playlist_duration_total_comes_from_the_database():
    db = _fake_liked_detail_db(
        total_count=1, oldest_created_at=_LIKE_ONE_ROW["created_at"], duration_total=999
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.json()["data"]["total_duration_seconds"] == 999
    db.rpc.assert_called_once_with(
        "get_liked_tracks_duration_total", {"p_user_id": _USER_ID}
    )


def test_get_liked_playlist_summary_failure_returns_upstream_error():
    _use_db(_fake_liked_detail_db(summary_error=APIError({"message": "down"})))
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_summary_timeout_returns_upstream_timeout():
    _use_db(_fake_liked_detail_db(summary_error=httpx.ReadTimeout("timed out")))
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_liked_playlist_latest_like_failure_returns_upstream_error():
    db = _fake_liked_detail_db(
        total_count=1,
        oldest_created_at=_LIKE_ONE_ROW["created_at"],
        latest_error=APIError({"message": "down"}),
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_latest_like_timeout_returns_upstream_timeout():
    db = _fake_liked_detail_db(
        total_count=1,
        oldest_created_at=_LIKE_ONE_ROW["created_at"],
        latest_error=httpx.ReadTimeout("timed out"),
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_liked_playlist_duration_failure_returns_upstream_error():
    db = _fake_liked_detail_db(
        total_count=1,
        oldest_created_at=_LIKE_ONE_ROW["created_at"],
        duration_error=APIError({"message": "down"}),
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_liked_playlist_duration_timeout_returns_upstream_timeout():
    db = _fake_liked_detail_db(
        total_count=1,
        oldest_created_at=_LIKE_ONE_ROW["created_at"],
        duration_error=httpx.ReadTimeout("timed out"),
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_liked_playlist_non_numeric_duration_returns_upstream_error():
    db = _fake_liked_detail_db(
        total_count=1, oldest_created_at=_LIKE_ONE_ROW["created_at"]
    )
    db.rpc.return_value.execute.return_value = MagicMock(data=None)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_unauthenticated_get_liked_playlist_returns_unauthorized():
    response = client.get("/playlists/liked")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_get_liked_playlist_route_takes_precedence_over_playlist_id():
    db = _fake_liked_detail_db(total_count=0, duration_total=0)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == "liked"
    assert "playlists" not in db.tables


# --- GET /playlists/liked/tracks (new) ----------------------------------

_LIKED_LINK_ONE_ID = "44440000-0000-0000-0000-000000000001"
_LIKED_LINK_TWO_ID = "44440000-0000-0000-0000-000000000002"

_PAGE_TRACK_ONE = {key: value for key, value in _TRACK_ONE.items() if key != "id"}
_PAGE_TRACK_TWO = {key: value for key, value in _TRACK_TWO.items() if key != "id"}


def _fake_liked_tracks_page_db(
    preceding_count=None,
    page_rows=None,
    page_count=0,
    track_rows=None,
    preceding_error=None,
    page_error=None,
    tracks_error=None,
):
    if page_rows is None:
        page_rows = []
    if track_rows is None:
        track_rows = []

    db = MagicMock()
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]

        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "user_likes":
            base = _chain(table_mock, "select", "eq", "is_")

            preceding_leaf = _chain(base, "or_", "limit")
            if preceding_error is not None:
                preceding_leaf.execute.side_effect = preceding_error
            else:
                preceding_leaf.execute.return_value = MagicMock(count=preceding_count)

            page_response = MagicMock(data=page_rows, count=page_count)
            no_cursor_leaf = _chain(base, "order", "order", "limit")
            cursor_leaf = _chain(base, "or_", "order", "order", "limit")
            if page_error is not None:
                no_cursor_leaf.execute.side_effect = page_error
                cursor_leaf.execute.side_effect = page_error
            else:
                no_cursor_leaf.execute.return_value = page_response
                cursor_leaf.execute.return_value = page_response
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


def test_get_liked_tracks_returns_first_page_in_like_order():
    rows = [
        {"track_id": _TRACK_ONE["track_id"], "created_at": _LIKE_ONE_ROW["created_at"]},
        {"track_id": _TRACK_TWO["track_id"], "created_at": _LIKE_TWO_ROW["created_at"]},
    ]
    db = _fake_liked_tracks_page_db(
        page_rows=rows, page_count=2, track_rows=[_TRACK_ONE, _TRACK_TWO]
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked/tracks")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "items": [
            {**_PAGE_TRACK_ONE, "position": 1},
            {**_PAGE_TRACK_TWO, "position": 2},
        ],
        "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 2},
    }

    table = db.tables["user_likes"]
    base = _chain(table, "select", "eq", "is_")
    base.order.assert_any_call("created_at", desc=False)
    base.order.return_value.order.assert_called_once_with("track_id", desc=False)
    tracks_table = db.tables["tracks"]
    tracks_table.select.return_value.in_.assert_called_once_with(
        "track_id", [_TRACK_ONE["track_id"], _TRACK_TWO["track_id"]]
    )


def test_get_liked_tracks_with_no_likes_is_an_empty_first_page():
    db = _fake_liked_tracks_page_db(page_rows=[], page_count=0)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked/tracks")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [],
            "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 0},
        },
    }
    assert "tracks" not in db.tables


def test_get_liked_tracks_items_do_not_expose_the_catalog_id():
    rows = [
        {"track_id": _TRACK_ONE["track_id"], "created_at": _LIKE_ONE_ROW["created_at"]}
    ]
    db = _fake_liked_tracks_page_db(
        page_rows=rows, page_count=1, track_rows=[_TRACK_ONE]
    )
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked/tracks")

    item = response.json()["data"]["items"][0]
    assert "id" not in item
    assert item["track_id"] == _TRACK_ONE["track_id"]


def test_get_liked_tracks_is_scoped_to_the_user_id_from_the_token():
    db = _fake_liked_tracks_page_db(page_rows=[], page_count=0)
    _use_db(db)
    _use_auth(user_id=_OTHER_USER_ID)

    client.get("/playlists/liked/tracks", params={"user_id": _USER_ID})

    table = db.tables["user_likes"]
    table.select.return_value.eq.assert_called_once_with("user_id", _OTHER_USER_ID)


@pytest.mark.parametrize("which", ["preceding_count", "page", "catalog"])
def test_get_liked_tracks_upstream_failure_returns_upstream_error(which):
    error = APIError({"message": "connection refused"})
    kwargs = {"preceding_count": {}, "page": {}, "catalog": {}}
    if which == "preceding_count":
        kwargs["preceding_count"] = {"preceding_error": error}
    elif which == "page":
        kwargs["page"] = {"page_error": error}
    else:
        rows = [
            {
                "track_id": _TRACK_ONE["track_id"],
                "created_at": _LIKE_ONE_ROW["created_at"],
            }
        ]
        kwargs["catalog"] = {"page_rows": rows, "page_count": 1, "tracks_error": error}

    merged = {**kwargs["preceding_count"], **kwargs["page"], **kwargs["catalog"]}
    db = _fake_liked_tracks_page_db(**merged)
    _use_db(db)
    _use_auth()

    params = {}
    if which == "preceding_count":
        cursor = Cursor(value=_LIKE_ONE_ROW["created_at"], id=_TRACK_ONE["track_id"])
        params["cursor"] = encode_cursor(cursor.value, cursor.id, _LIKED_TRACKS_SORT)

    response = client.get("/playlists/liked/tracks", params=params)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("which", ["preceding_count", "page", "catalog"])
def test_get_liked_tracks_upstream_timeout_returns_upstream_timeout(which):
    error = httpx.ReadTimeout("timed out")
    kwargs = {"preceding_count": {}, "page": {}, "catalog": {}}
    if which == "preceding_count":
        kwargs["preceding_count"] = {"preceding_error": error}
    elif which == "page":
        kwargs["page"] = {"page_error": error}
    else:
        rows = [
            {
                "track_id": _TRACK_ONE["track_id"],
                "created_at": _LIKE_ONE_ROW["created_at"],
            }
        ]
        kwargs["catalog"] = {"page_rows": rows, "page_count": 1, "tracks_error": error}

    merged = {**kwargs["preceding_count"], **kwargs["page"], **kwargs["catalog"]}
    db = _fake_liked_tracks_page_db(**merged)
    _use_db(db)
    _use_auth()

    params = {}
    if which == "preceding_count":
        cursor = Cursor(value=_LIKE_ONE_ROW["created_at"], id=_TRACK_ONE["track_id"])
        params["cursor"] = encode_cursor(cursor.value, cursor.id, _LIKED_TRACKS_SORT)

    response = client.get("/playlists/liked/tracks", params=params)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


@pytest.mark.parametrize("limit", [0, -5, 101, "abc"])
def test_get_liked_tracks_invalid_limit_returns_invalid_request(limit):
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked/tracks", params={"limit": limit})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_get_liked_tracks_garbage_cursor_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked/tracks", params={"cursor": "???"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


def test_get_liked_tracks_cursor_from_another_endpoint_returns_invalid_cursor():
    # A cursor from GET /playlists (a different sort key entirely), not from
    # GET /likes: the likes cursor is actually valid here (correction 6),
    # since it is the same order over the same rows.
    db = MagicMock()
    _use_db(db)
    _use_auth()
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", _PLAYLIST_ID, _LIST_SORT)

    response = client.get("/playlists/liked/tracks", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


def test_unauthenticated_get_liked_tracks_returns_unauthorized():
    response = client.get("/playlists/liked/tracks")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_get_liked_tracks_route_takes_precedence_over_playlist_id_tracks():
    db = _fake_liked_tracks_page_db(page_rows=[], page_count=0)
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/liked/tracks")

    assert response.status_code == 200
    assert "playlists" not in db.tables


def test_walking_liked_tracks_with_limit_two_returns_every_track_once():
    rows = [
        {"track_id": f"t{index}", "created_at": f"2026-01-01T00:00:{index:02d}+00:00"}
        for index in range(5)
    ]
    catalog = {
        row["track_id"]: {**_TRACK_ONE, "track_id": row["track_id"]} for row in rows
    }
    _use_auth()

    cursor = None
    seen = []
    positions = []
    while True:
        page_rows, preceding = _page_and_preceding(
            rows, lambda r: (r["created_at"], r["track_id"]), cursor, 2
        )
        db = _fake_liked_tracks_page_db(
            preceding_count=preceding,
            page_rows=page_rows,
            page_count=len(rows) if cursor is None else None,
            track_rows=[catalog[row["track_id"]] for row in page_rows],
        )
        _use_db(db)
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = encode_cursor(
                cursor.value, cursor.id, _LIKED_TRACKS_SORT
            )

        response = client.get("/playlists/liked/tracks", params=params)
        assert response.status_code == 200
        body = response.json()["data"]
        seen.extend(item["track_id"] for item in body["items"])
        positions.extend(item["position"] for item in body["items"])
        if not body["page"]["has_more"]:
            break
        cursor = decode_cursor(body["page"]["next_cursor"], _LIKED_TRACKS_SORT)

    assert seen == [f"t{index}" for index in range(5)]
    assert positions == [1, 2, 3, 4, 5]


def test_get_liked_tracks_cursored_page_numbers_positions_after_the_preceding_rows():
    rows = [
        {
            "track_id": _TRACK_ONE["track_id"],
            "created_at": _LIKE_ONE_ROW["created_at"],
        },
        {
            "track_id": _TRACK_TWO["track_id"],
            "created_at": _LIKE_TWO_ROW["created_at"],
        },
    ]
    db = _fake_liked_tracks_page_db(
        preceding_count=50,
        page_rows=rows,
        page_count=None,
        track_rows=[_TRACK_ONE, _TRACK_TWO],
    )
    _use_db(db)
    _use_auth()
    raw_cursor = encode_cursor(
        "2025-12-31T00:00:00Z", _TRACK_ONE["track_id"], _LIKED_TRACKS_SORT
    )
    # decode_cursor re-normalizes the timestamp (e.g. "Z" -> "+00:00"), and
    # the service filters on that normalized value, not the one encoded.
    cursor = decode_cursor(raw_cursor, _LIKED_TRACKS_SORT)

    response = client.get(
        "/playlists/liked/tracks",
        params={"limit": 2, "cursor": raw_cursor},
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert [item["position"] for item in body["items"]] == [51, 52]
    assert body["page"]["total"] is None

    table = db.tables["user_likes"]
    base = _chain(table, "select", "eq", "is_")
    base.or_.assert_any_call(through_cursor_filter(_LIKED_TRACKS_SORT, cursor))
    base.or_.assert_any_call(keyset_filter(_LIKED_TRACKS_SORT, cursor))


# --- GET /playlists/{playlist_id} ---------------------------------------

# Since #139 this endpoint no longer reads or returns tracks: it is
# metadata plus aggregates. See "GET /playlists/{playlist_id}/tracks"
# below for the paginated track list.


def _fake_playlist_detail_db(
    playlist_rows=None,
    track_count=None,
    duration_total=None,
    playlist_error=None,
    count_error=None,
    duration_error=None,
):
    if track_count is None:
        track_count = 0
    if duration_total is None:
        duration_total = 0

    db = MagicMock()
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]

        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "playlists":
            _pin_playlist(table_mock, playlist_rows, playlist_error)
        elif name == "playlist_tracks":
            query = table_mock.select.return_value.eq.return_value.limit.return_value
            if count_error is not None:
                query.execute.side_effect = count_error
            else:
                query.execute.return_value = MagicMock(data=[], count=track_count)
        return table_mock

    db.table.side_effect = table_side_effect
    db.tables = tables
    _pin(db.rpc.return_value, data=duration_total, error=duration_error)
    return db


def test_get_playlist_returns_metadata_and_aggregates_without_tracks():
    db = _fake_playlist_detail_db(track_count=2, duration_total=420)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert set(body["data"].keys()) == {
        "id",
        "owner_id",
        "title",
        "description",
        "is_public",
        "created_at",
        "updated_at",
        "total_count",
        "total_duration_seconds",
    }
    assert body["data"] == {
        **_PLAYLIST_ROW,
        "total_count": 2,
        "total_duration_seconds": 420,
    }
    db.tables["playlist_tracks"].select.assert_called_once_with("id", count="exact")


def test_get_playlist_with_no_tracks_reports_zero_totals():
    db = _fake_playlist_detail_db(track_count=0, duration_total=0)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["total_count"] == 0
    assert body["data"]["total_duration_seconds"] == 0
    assert "tracks" not in body["data"]
    assert "has_more" not in body["data"]


def test_get_playlist_reads_a_null_is_public_as_false():
    db = _fake_playlist_detail_db(playlist_rows=[{**_PLAYLIST_ROW, "is_public": None}])
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["is_public"] is False


def test_get_playlist_unknown_id_returns_playlist_not_found():
    db = _fake_playlist_detail_db(playlist_rows=[])
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    assert "playlist_tracks" not in db.tables


def test_get_playlist_owned_by_another_user_returns_playlist_not_found():
    db = _fake_playlist_detail_db(playlist_rows=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
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


def test_get_playlist_asks_the_database_for_the_duration_total():
    db = _fake_playlist_detail_db(track_count=2, duration_total=999)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.json()["data"]["total_duration_seconds"] == 999
    db.rpc.assert_called_once_with(
        "get_playlist_duration_total", {"p_playlist_id": _PLAYLIST_ID}
    )


def test_get_playlist_duration_failure_returns_upstream_error():
    db = _fake_playlist_detail_db(duration_error=APIError({"message": "down"}))
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_duration_timeout_returns_upstream_timeout():
    db = _fake_playlist_detail_db(duration_error=httpx.ReadTimeout("timed out"))
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_playlist_null_duration_total_returns_upstream_error():
    db = _fake_playlist_detail_db()
    db.rpc.return_value.execute.return_value = MagicMock(data=None)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_boolean_duration_total_returns_upstream_error():
    db = _fake_playlist_detail_db()
    db.rpc.return_value.execute.return_value = MagicMock(data=True)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_lookup_failure_returns_upstream_error():
    db = _fake_playlist_detail_db(playlist_error=APIError({"message": "down"}))
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_lookup_timeout_returns_upstream_timeout():
    db = _fake_playlist_detail_db(playlist_error=httpx.ReadTimeout("timed out"))
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_playlist_count_failure_returns_upstream_error():
    db = _fake_playlist_detail_db(count_error=APIError({"message": "down"}))
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_count_timeout_returns_upstream_timeout():
    db = _fake_playlist_detail_db(count_error=httpx.ReadTimeout("timed out"))
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_unauthenticated_get_request_returns_unauthorized():
    response = client.get(f"/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- GET /playlists/{playlist_id}/tracks (new) ---------------------------

_ENTRY_ONE_ID = "33330000-0000-0000-0000-000000000001"
_ENTRY_TWO_ID = "33330000-0000-0000-0000-000000000002"


def _page_and_preceding(rows, key_func, cursor, limit):
    """Pure-python mirror of what the database would answer for one page:
    rows sorted ascending by key_func -> (sort value, id tiebreaker), sliced
    to the fetch_limit past the cursor, and the count of rows at or before
    it. Used only by the tests that walk several pages against a mutable
    in-memory row list (the criterion 9 and reorder tests below) -- every
    other test pins a fixed response instead."""
    ordered = sorted(rows, key=key_func)
    if cursor is None:
        return ordered[: limit + 1], 0

    cursor_key = (cursor.value, cursor.id)
    preceding = sum(1 for row in ordered if key_func(row) <= cursor_key)
    remaining = [row for row in ordered if key_func(row) > cursor_key]
    return remaining[: limit + 1], preceding


def _fake_tracks_page_db(
    playlist_rows=None,
    preceding_count=None,
    page_rows=None,
    page_count=0,
    track_rows=None,
    playlist_error=None,
    preceding_error=None,
    page_error=None,
    tracks_error=None,
):
    if page_rows is None:
        page_rows = []
    if track_rows is None:
        track_rows = []

    db = MagicMock()
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]

        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "playlists":
            _pin_playlist(table_mock, playlist_rows, playlist_error)
        elif name == "playlist_tracks":
            base = _chain(table_mock, "select", "eq")

            preceding_leaf = _chain(base, "or_", "limit")
            if preceding_error is not None:
                preceding_leaf.execute.side_effect = preceding_error
            else:
                preceding_leaf.execute.return_value = MagicMock(count=preceding_count)

            page_response = MagicMock(data=page_rows, count=page_count)
            no_cursor_leaf = _chain(base, "order", "order", "limit")
            cursor_leaf = _chain(base, "or_", "order", "order", "limit")
            if page_error is not None:
                no_cursor_leaf.execute.side_effect = page_error
                cursor_leaf.execute.side_effect = page_error
            else:
                no_cursor_leaf.execute.return_value = page_response
                cursor_leaf.execute.return_value = page_response
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


def test_get_playlist_tracks_returns_first_page_in_order_key_order():
    rows = [
        {"id": _ENTRY_ONE_ID, "track_id": _TRACK_ONE_ID, "order_key": "a0"},
        {"id": _ENTRY_TWO_ID, "track_id": _TRACK_TWO_ID, "order_key": "a1"},
    ]
    db = _fake_tracks_page_db(
        page_rows=rows, page_count=2, track_rows=[_TRACK_ONE, _TRACK_TWO]
    )
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "items": [
            {**_PAGE_TRACK_ONE, "position": 1},
            {**_PAGE_TRACK_TWO, "position": 2},
        ],
        "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 2},
    }

    table = db.tables["playlist_tracks"]
    table.select.assert_called_once_with("id, track_id, order_key", count="exact")
    table.select.return_value.eq.assert_called_once_with("playlist_id", _PLAYLIST_ID)
    base = _chain(table, "select", "eq")
    base.order.assert_called_once_with("order_key", desc=False)
    base.order.return_value.order.assert_called_once_with("id", desc=False)
    tracks_table = db.tables["tracks"]
    tracks_table.select.return_value.in_.assert_called_once_with(
        "id", [_TRACK_ONE_ID, _TRACK_TWO_ID]
    )


def test_get_playlist_tracks_empty_playlist_is_an_empty_first_page():
    db = _fake_tracks_page_db(page_rows=[], page_count=0)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [],
            "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 0},
        },
    }
    assert "tracks" not in db.tables


def test_get_playlist_tracks_unknown_playlist_returns_playlist_not_found():
    db = _fake_tracks_page_db(playlist_rows=[])
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}
    assert "playlist_tracks" not in db.tables


def test_get_playlist_tracks_owned_by_another_user_returns_playlist_not_found():
    db = _fake_tracks_page_db(playlist_rows=[_OTHER_PLAYLIST_ROW])
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_get_playlist_tracks_malformed_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get("/playlists/not-a-uuid/tracks")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


@pytest.mark.parametrize("limit", [0, -5, 101, "abc"])
def test_get_playlist_tracks_invalid_limit_returns_invalid_request(limit):
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params={"limit": limit})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_get_playlist_tracks_garbage_cursor_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params={"cursor": "???"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


def test_get_playlist_tracks_cursor_from_another_endpoint_returns_invalid_cursor():
    db = MagicMock()
    _use_db(db)
    _use_auth()
    cursor = encode_cursor("2026-01-01T00:00:00+00:00", _PLAYLIST_ID, _LIST_SORT)

    response = client.get(
        f"/playlists/{_PLAYLIST_ID}/tracks", params={"cursor": cursor}
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    db.table.assert_not_called()


@pytest.mark.parametrize("which", ["lookup", "preceding_count", "page", "catalog"])
def test_get_playlist_tracks_upstream_failure_returns_upstream_error(which):
    error = APIError({"message": "connection refused"})
    extra = {}
    if which == "lookup":
        extra["playlist_error"] = error
    elif which == "preceding_count":
        extra["preceding_error"] = error
    elif which == "page":
        extra["page_error"] = error
    else:
        extra["page_rows"] = [
            {"id": _ENTRY_ONE_ID, "track_id": _TRACK_ONE_ID, "order_key": "a0"}
        ]
        extra["page_count"] = 1
        extra["tracks_error"] = error

    db = _fake_tracks_page_db(**extra)
    _use_db(db)
    _use_auth()

    params = {}
    if which == "preceding_count":
        cursor = Cursor(value="a0", id=_ENTRY_ONE_ID)
        params["cursor"] = encode_cursor(cursor.value, cursor.id, _TRACKS_SORT)

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params=params)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("which", ["lookup", "preceding_count", "page", "catalog"])
def test_get_playlist_tracks_upstream_timeout_returns_upstream_timeout(which):
    error = httpx.ReadTimeout("timed out")
    extra = {}
    if which == "lookup":
        extra["playlist_error"] = error
    elif which == "preceding_count":
        extra["preceding_error"] = error
    elif which == "page":
        extra["page_error"] = error
    else:
        extra["page_rows"] = [
            {"id": _ENTRY_ONE_ID, "track_id": _TRACK_ONE_ID, "order_key": "a0"}
        ]
        extra["page_count"] = 1
        extra["tracks_error"] = error

    db = _fake_tracks_page_db(**extra)
    _use_db(db)
    _use_auth()

    params = {}
    if which == "preceding_count":
        cursor = Cursor(value="a0", id=_ENTRY_ONE_ID)
        params["cursor"] = encode_cursor(cursor.value, cursor.id, _TRACKS_SORT)

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params=params)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_playlist_tracks_cursored_page_numbers_positions_after_the_preceding_rows():
    rows = [
        {"id": _ENTRY_ONE_ID, "track_id": _TRACK_ONE_ID, "order_key": "a50"},
        {"id": _ENTRY_TWO_ID, "track_id": _TRACK_TWO_ID, "order_key": "a51"},
    ]
    db = _fake_tracks_page_db(
        preceding_count=50,
        page_rows=rows,
        page_count=None,
        track_rows=[_TRACK_ONE, _TRACK_TWO],
    )
    _use_db(db)
    _use_auth()
    cursor = Cursor(value="a49", id=_ENTRY_ONE_ID)

    response = client.get(
        f"/playlists/{_PLAYLIST_ID}/tracks",
        params={
            "limit": 2,
            "cursor": encode_cursor(cursor.value, cursor.id, _TRACKS_SORT),
        },
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert [item["position"] for item in body["items"]] == [51, 52]
    assert body["page"]["total"] is None

    table = db.tables["playlist_tracks"]
    base = _chain(table, "select", "eq")
    base.or_.assert_any_call(through_cursor_filter(_TRACKS_SORT, cursor))
    base.or_.assert_any_call(keyset_filter(_TRACKS_SORT, cursor))


def test_get_playlist_tracks_first_page_does_not_count_preceding_rows():
    db = _fake_tracks_page_db(page_rows=[], page_count=0)
    _use_db(db)
    _use_auth()

    client.get(f"/playlists/{_PLAYLIST_ID}/tracks")

    table = db.tables["playlist_tracks"]
    base = _chain(table, "select", "eq")
    base.or_.assert_not_called()


def test_get_playlist_tracks_missing_preceding_count_returns_upstream_error():
    db = _fake_tracks_page_db(preceding_count=None)
    _use_db(db)
    _use_auth()
    cursor = Cursor(value="a0", id=_ENTRY_ONE_ID)

    response = client.get(
        f"/playlists/{_PLAYLIST_ID}/tracks",
        params={"cursor": encode_cursor(cursor.value, cursor.id, _TRACKS_SORT)},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_playlist_tracks_first_page_with_more_rows_has_next_cursor():
    rows = [
        {"id": _ENTRY_ONE_ID, "track_id": _TRACK_ONE_ID, "order_key": "a0"},
        {"id": _ENTRY_TWO_ID, "track_id": _TRACK_TWO_ID, "order_key": "a1"},
    ]
    db = _fake_tracks_page_db(
        page_rows=rows, page_count=2, track_rows=[_TRACK_ONE, _TRACK_TWO]
    )
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params={"limit": 1})

    body = response.json()["data"]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"] is not None
    assert len(body["items"]) == 1


def test_get_playlist_tracks_last_page_has_no_next_cursor():
    rows = [{"id": _ENTRY_ONE_ID, "track_id": _TRACK_ONE_ID, "order_key": "a0"}]
    db = _fake_tracks_page_db(page_rows=rows, page_count=1, track_rows=[_TRACK_ONE])
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params={"limit": 1})

    body = response.json()["data"]
    assert body["page"]["has_more"] is False
    assert body["page"]["next_cursor"] is None


def test_get_playlist_tracks_uses_the_user_id_from_the_token():
    db = _fake_tracks_page_db(playlist_rows=[_PLAYLIST_ROW])
    _use_db(db)
    _use_auth(user_id=_OTHER_USER_ID)

    response = client.get(
        f"/playlists/{_PLAYLIST_ID}/tracks", params={"user_id": _USER_ID}
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_unauthenticated_get_playlist_tracks_returns_unauthorized():
    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_get_playlist_tracks_items_do_not_expose_the_catalog_id():
    rows = [{"id": _ENTRY_ONE_ID, "track_id": _TRACK_ONE_ID, "order_key": "a0"}]
    db = _fake_tracks_page_db(page_rows=rows, page_count=1, track_rows=[_TRACK_ONE])
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks")

    item = response.json()["data"]["items"][0]
    assert "id" not in item
    assert item["track_id"] == _TRACK_ONE["track_id"]


def test_get_playlist_tracks_route_does_not_collide_with_post():
    # A GET on the same path the POST/POST-bulk endpoints use: different
    # methods, so no ambiguity, exercised here as a smoke test of the
    # route declaration order.
    db = _fake_tracks_page_db(page_rows=[], page_count=0)
    _use_db(db)
    _use_auth()

    response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks")

    assert response.status_code == 200


def test_walking_a_playlist_with_limit_one_returns_every_track_once():
    # "track_id" here is playlist_tracks.track_id, the catalog uuid the
    # join key is on -- not the provider id. The catalog row's own
    # "track_id" (below) is the provider id, "t{index}", asserted against.
    rows = [
        {
            "id": f"{index:08d}-1111-1111-1111-111111111111",
            "track_id": f"{index:08d}-0000-0000-0000-000000000000",
            "order_key": f"a{index}",
        }
        for index in range(5)
    ]
    catalog = {
        row["track_id"]: {**_TRACK_ONE, "id": row["track_id"], "track_id": f"t{index}"}
        for index, row in enumerate(rows)
    }
    _use_auth()

    cursor = None
    seen = []
    positions = []
    while True:
        page_rows, preceding = _page_and_preceding(
            rows, lambda r: (r["order_key"], r["id"]), cursor, 1
        )
        db = _fake_tracks_page_db(
            playlist_rows=[_PLAYLIST_ROW],
            preceding_count=preceding,
            page_rows=page_rows,
            page_count=len(rows) if cursor is None else None,
            track_rows=[catalog[row["track_id"]] for row in page_rows],
        )
        _use_db(db)
        params = {"limit": 1}
        if cursor is not None:
            params["cursor"] = encode_cursor(cursor.value, cursor.id, _TRACKS_SORT)

        response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params=params)
        assert response.status_code == 200
        body = response.json()["data"]
        seen.extend(item["track_id"] for item in body["items"])
        positions.extend(item["position"] for item in body["items"])
        if not body["page"]["has_more"]:
            break
        cursor = decode_cursor(body["page"]["next_cursor"], _TRACKS_SORT)

    assert seen == [f"t{index}" for index in range(5)]
    assert positions == [1, 2, 3, 4, 5]


def test_walking_a_playlist_with_limit_two_returns_every_track_once():
    rows = [
        {
            "id": f"{index:08d}-2222-2222-2222-222222222222",
            "track_id": f"{index:08d}-0000-0000-0000-000000000000",
            "order_key": f"a{index}",
        }
        for index in range(5)
    ]
    catalog = {
        row["track_id"]: {**_TRACK_ONE, "id": row["track_id"], "track_id": f"t{index}"}
        for index, row in enumerate(rows)
    }
    _use_auth()

    cursor = None
    seen = []
    while True:
        page_rows, preceding = _page_and_preceding(
            rows, lambda r: (r["order_key"], r["id"]), cursor, 2
        )
        db = _fake_tracks_page_db(
            playlist_rows=[_PLAYLIST_ROW],
            preceding_count=preceding,
            page_rows=page_rows,
            page_count=len(rows) if cursor is None else None,
            track_rows=[catalog[row["track_id"]] for row in page_rows],
        )
        _use_db(db)
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = encode_cursor(cursor.value, cursor.id, _TRACKS_SORT)

        response = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params=params)
        assert response.status_code == 200
        body = response.json()["data"]
        seen.extend(item["track_id"] for item in body["items"])
        if not body["page"]["has_more"]:
            break
        cursor = decode_cursor(body["page"]["next_cursor"], _TRACKS_SORT)

    assert seen == [f"t{index}" for index in range(5)]


def test_reorder_moved_track_lands_before_cursor_is_skipped_once():
    # A..E, order_key "10".."50". Page 1 (limit=2) reads A, B. Before page 2
    # is requested, E is moved to "15" -- between A and B, i.e. behind the
    # cursor. Decision 3: the cursor stays valid, C and D (not moved) are
    # each seen exactly once, and E -- moved behind a cursor that already
    # passed it -- is never seen again. This is the documented limitation
    # of keyset pagination over a mutable sort column, not a bug.
    # "track_id" is playlist_tracks.track_id, the catalog uuid the join key
    # is on; the catalog row's own "track_id" ("t{index}") is the provider
    # id the test asserts sequences against.
    rows = [
        {
            "id": f"{index:08d}-3333-3333-3333-333333333333",
            "track_id": f"{index:08d}-0000-0000-0000-000000000000",
            "order_key": key,
        }
        for index, key in enumerate(["10", "20", "30", "40", "50"])
    ]
    catalog = {
        row["track_id"]: {**_TRACK_ONE, "id": row["track_id"], "track_id": f"t{index}"}
        for index, row in enumerate(rows)
    }
    _use_auth()

    page1_rows, _ = _page_and_preceding(
        rows, lambda r: (r["order_key"], r["id"]), None, 2
    )
    db1 = _fake_tracks_page_db(
        playlist_rows=[_PLAYLIST_ROW],
        page_rows=page1_rows,
        page_count=len(rows),
        track_rows=[catalog[row["track_id"]] for row in page1_rows],
    )
    _use_db(db1)
    response1 = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params={"limit": 2})
    body1 = response1.json()["data"]
    assert [item["track_id"] for item in body1["items"]] == ["t0", "t1"]
    cursor = decode_cursor(body1["page"]["next_cursor"], _TRACKS_SORT)

    # Move E (index 4) behind the cursor.
    rows[4]["order_key"] = "15"

    seen = [item["track_id"] for item in body1["items"]]
    while cursor is not None:
        page_rows, preceding = _page_and_preceding(
            rows, lambda r: (r["order_key"], r["id"]), cursor, 2
        )
        db = _fake_tracks_page_db(
            playlist_rows=[_PLAYLIST_ROW],
            preceding_count=preceding,
            page_rows=page_rows,
            page_count=None,
            track_rows=[catalog[row["track_id"]] for row in page_rows],
        )
        _use_db(db)
        response = client.get(
            f"/playlists/{_PLAYLIST_ID}/tracks",
            params={
                "limit": 2,
                "cursor": encode_cursor(cursor.value, cursor.id, _TRACKS_SORT),
            },
        )
        assert response.status_code == 200
        body = response.json()["data"]
        seen.extend(item["track_id"] for item in body["items"])
        cursor = (
            decode_cursor(body["page"]["next_cursor"], _TRACKS_SORT)
            if body["page"]["next_cursor"]
            else None
        )

    assert seen == ["t0", "t1", "t2", "t3"]
    assert "t4" not in seen


def test_reorder_moved_track_lands_after_cursor_is_duplicated_once():
    # A..E, order_key "10".."50". Page 1 (limit=2) reads A, B. Before page 2
    # is requested, A is moved to "25" -- between B and C, i.e. ahead of
    # the cursor. Decision 3: A is seen again on the next page (duplicate),
    # while B, C, D, E are each seen exactly once.
    # "track_id" is playlist_tracks.track_id, the catalog uuid the join key
    # is on; the catalog row's own "track_id" ("t{index}") is the provider
    # id the test asserts sequences against.
    rows = [
        {
            "id": f"{index:08d}-3333-3333-3333-333333333333",
            "track_id": f"{index:08d}-0000-0000-0000-000000000000",
            "order_key": key,
        }
        for index, key in enumerate(["10", "20", "30", "40", "50"])
    ]
    catalog = {
        row["track_id"]: {**_TRACK_ONE, "id": row["track_id"], "track_id": f"t{index}"}
        for index, row in enumerate(rows)
    }
    _use_auth()

    page1_rows, _ = _page_and_preceding(
        rows, lambda r: (r["order_key"], r["id"]), None, 2
    )
    db1 = _fake_tracks_page_db(
        playlist_rows=[_PLAYLIST_ROW],
        page_rows=page1_rows,
        page_count=len(rows),
        track_rows=[catalog[row["track_id"]] for row in page1_rows],
    )
    _use_db(db1)
    response1 = client.get(f"/playlists/{_PLAYLIST_ID}/tracks", params={"limit": 2})
    body1 = response1.json()["data"]
    assert [item["track_id"] for item in body1["items"]] == ["t0", "t1"]
    cursor = decode_cursor(body1["page"]["next_cursor"], _TRACKS_SORT)

    # Move A (index 0) ahead of the cursor.
    rows[0]["order_key"] = "25"

    seen = [item["track_id"] for item in body1["items"]]
    while cursor is not None:
        page_rows, preceding = _page_and_preceding(
            rows, lambda r: (r["order_key"], r["id"]), cursor, 2
        )
        db = _fake_tracks_page_db(
            playlist_rows=[_PLAYLIST_ROW],
            preceding_count=preceding,
            page_rows=page_rows,
            page_count=None,
            track_rows=[catalog[row["track_id"]] for row in page_rows],
        )
        _use_db(db)
        response = client.get(
            f"/playlists/{_PLAYLIST_ID}/tracks",
            params={
                "limit": 2,
                "cursor": encode_cursor(cursor.value, cursor.id, _TRACKS_SORT),
            },
        )
        assert response.status_code == 200
        body = response.json()["data"]
        seen.extend(item["track_id"] for item in body["items"])
        cursor = (
            decode_cursor(body["page"]["next_cursor"], _TRACKS_SORT)
            if body["page"]["next_cursor"]
            else None
        )

    assert seen == ["t0", "t1", "t0", "t2", "t3", "t4"]
    assert seen.count("t0") == 2
    for track_id in ("t1", "t2", "t3", "t4"):
        assert seen.count(track_id) == 1


# --- GET /public/playlists/{playlist_id} (catalog batching) --------------

# These two tests used to live under GET /playlists/{playlist_id}: since
# #139 that endpoint no longer reads the track catalog at all, and the two
# new paginated endpoints cap their fetch at MAX_LIMIT + 1 = 101 rows,
# under _TRACK_BATCH_SIZE (150), so they can never trigger a batched catalog
# read. get_public_playlist() is the only remaining caller of
# _list_playlist_tracks() (cap 1000), so it is the only reachable place left
# to exercise the batching in _tracks_by(). test/routes/test_public.py is
# frozen for this issue (criterion 11), so these live here instead.


def _batched_public_playlist_rows(count):
    entry_rows = [
        {"track_id": f"{index:08d}-0000-0000-0000-000000000000"}
        for index in range(count)
    ]
    track_rows = [
        {**_TRACK_ONE, "id": row["track_id"], "track_id": f"t{index}"}
        for index, row in enumerate(entry_rows)
    ]
    return entry_rows, track_rows


def _fake_public_playlist_batch_db(entry_rows, track_rows):
    db = MagicMock()
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]
        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "playlists":
            query = table_mock.select.return_value.eq.return_value.eq.return_value
            query.execute.return_value = MagicMock(
                data=[{**_PLAYLIST_ROW, "is_public": True}]
            )
        elif name == "playlist_tracks":
            query = table_mock.select.return_value.eq.return_value.order.return_value.limit.return_value
            query.execute.return_value = MagicMock(
                data=entry_rows, count=len(entry_rows)
            )
        elif name == "tracks":
            query = table_mock.select.return_value.in_.return_value
            query.execute.return_value = MagicMock(data=track_rows)
        return table_mock

    db.table.side_effect = table_side_effect
    db.tables = tables
    db.rpc.return_value.execute.return_value = MagicMock(data=0)
    return db


def test_public_playlist_still_reads_the_catalog_in_batches():
    entry_rows, track_rows = _batched_public_playlist_rows(200)
    db = _fake_public_playlist_batch_db(entry_rows, track_rows)
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    in_mock = db.tables["tracks"].select.return_value.in_
    assert in_mock.call_count == 2
    assert len(in_mock.call_args_list[0].args[1]) == 150
    assert len(in_mock.call_args_list[1].args[1]) == 50


def test_public_playlist_merges_batched_catalog_rows_in_order_key_order():
    entry_rows, track_rows = _batched_public_playlist_rows(200)
    db = _fake_public_playlist_batch_db(entry_rows, track_rows)
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    tracks = response.json()["data"]["tracks"]
    assert len(tracks) == 200
    assert [track["position"] for track in tracks] == list(range(1, 201))
    assert [track["track_id"] for track in tracks] == [
        f"t{index}" for index in range(200)
    ]


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
    # on the service-role client. p_order_key is "a0": an empty playlist
    # (the fixture's default last_key_rows) has no last key to compute
    # after.
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
            "p_order_key": "a0",
        },
    )


def test_add_track_to_an_empty_playlist_sends_the_first_key():
    db = _fake_add_db(last_key_rows=[])
    _use_db(db)
    _use_auth()

    client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert db.rpc.call_args.args[1]["p_order_key"] == "a0"


def test_add_track_sends_a_key_after_the_last_one():
    db = _fake_add_db(last_key_rows=[{"order_key": "a1"}])
    _use_db(db)
    _use_auth()

    client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert db.rpc.call_args.args[1]["p_order_key"] == "a2"
    db.tables[
        "playlist_tracks"
    ].select.return_value.eq.return_value.order.assert_called_once_with(
        "order_key", desc=True
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
    # The RPC computes it under its own lock right after the insert
    # (#137) -- it is not a stored column -- so the service neither
    # computes it nor writes the link itself: the only touch of
    # playlist_tracks from Python is the read of the last order_key.
    db = _fake_add_db()
    _use_db(db)
    _use_auth()

    client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert "position" not in db.rpc.call_args.args[1]
    playlist_tracks = db.tables["playlist_tracks"]
    playlist_tracks.select.assert_called_once_with("order_key")
    playlist_tracks.insert.assert_not_called()
    playlist_tracks.update.assert_not_called()
    playlist_tracks.upsert.assert_not_called()


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
    # Not retried: track_already_in_playlist is not order_key_conflict.
    assert db.rpc.call_count == 1


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


def test_add_track_last_key_read_failure_returns_upstream_error():
    db = _fake_add_db(last_key_error=APIError({"message": "connection refused"}))
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    db.rpc.assert_not_called()


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


def test_add_track_retries_with_a_fresh_key_after_an_order_key_conflict():
    db = _fake_add_db(
        rpc_responses=[
            {"ok": False, "error": "order_key_conflict"},
            {"ok": True, "id": _LINK_ROW_ID, "position": 3},
        ],
    )
    # A pre-seeded table mock, bypassing _fake_add_db's own (single-value)
    # configuration for playlist_tracks: the lazy table_side_effect in
    # _fake_multi_table_db returns this exact mock on the first
    # db.table("playlist_tracks") call instead.
    playlist_tracks = MagicMock()
    playlist_tracks.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.side_effect = [
        MagicMock(data=[{"order_key": "a1"}]),
        MagicMock(data=[{"order_key": "a2"}]),
    ]
    db.tables["playlist_tracks"] = playlist_tracks
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 200
    assert response.json()["data"]["position"] == 3
    assert db.rpc.call_count == 2
    first_args, second_args = (call.args[1] for call in db.rpc.call_args_list)
    assert first_args["p_order_key"] == "a2"
    assert second_args["p_order_key"] == "a3"


def test_add_track_gives_up_with_conflict_after_three_order_key_conflicts():
    db = _fake_add_db(
        rpc_responses=[{"ok": False, "error": "order_key_conflict"}] * 3,
    )
    playlist_tracks = MagicMock()
    playlist_tracks.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.side_effect = [
        MagicMock(data=[{"order_key": f"a{index}"}]) for index in range(3)
    ]
    db.tables["playlist_tracks"] = playlist_tracks
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "order_key_conflict"}
    assert db.rpc.call_count == 3
    assert (
        playlist_tracks.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.call_count
        == 3
    )


def test_add_track_order_key_conflict_then_duplicate_returns_track_already_in_playlist():
    # A non-order_key_conflict refusal on a later attempt follows its own
    # path (409 track_already_in_playlist here) rather than being retried
    # again.
    db = _fake_add_db(
        rpc_responses=[
            {"ok": False, "error": "order_key_conflict"},
            {"ok": False, "error": "track_already_in_playlist"},
        ],
    )
    _use_db(db)
    _use_auth()

    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "track_already_in_playlist"}
    assert db.rpc.call_count == 2


def test_unauthenticated_add_track_request_returns_unauthorized():
    response = client.post(f"/playlists/{_PLAYLIST_ID}/tracks", json=_ADD_ONE_BODY)

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


# --- POST /playlists/{playlist_id}/tracks/bulk ------------------------


def test_bulk_add_links_the_whole_batch_with_one_call():
    # The batch is one statement now: every track goes out in a single
    # add_playlist_tracks_bulk call, in the order they were sent. The keys
    # are "a0", "a1": an empty playlist (the fixture's default
    # last_key_rows) has no last key to compute after.
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
    assert args["p_order_keys"] == ["a0", "a1"]


def test_bulk_add_sends_one_key_per_unique_track_after_the_last_one():
    db = _fake_add_db(
        upsert_rows=[_TRACK_ONE, _TRACK_TWO],
        rpc_data=_bulk_rpc(added=1, skipped=1),
        last_key_rows=[{"order_key": "a1"}],
    )
    _use_db(db)
    _use_auth()

    client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    # Same length and order as p_track_ids -- two unique tracks after
    # dedupe -- even though the RPC will skip one of them.
    args = _bulk_call(db)[1]
    assert len(args["p_track_ids"]) == 2
    assert args["p_order_keys"] == ["a2", "a3"]


def test_bulk_add_takes_the_adding_user_from_the_token():
    db = _fake_add_db(rpc_data=_bulk_rpc(added=1))
    _use_db(db)
    _use_auth()

    client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert _bulk_call(db)[1]["p_added_by"] == _USER_ID


def test_bulk_add_resolves_provider_ids_through_the_catalog_write_alone():
    # The RPC takes catalog uuids and the upsert already returns them, so
    # the endpoint reads no existing links — deciding what to skip is the
    # RPC's job, not a query this service runs first. The only read of
    # playlist_tracks is the last order_key, not a provider-id lookup.
    db = _fake_add_db(upsert_rows=[_TRACK_ONE, _TRACK_TWO], rpc_data=_bulk_rpc(added=2))
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk",
        json={"tracks": [_ADD_ONE_BODY, _ADD_TWO_BODY]},
    )

    assert response.status_code == 200
    db.tables["playlist_tracks"].select.assert_called_once_with("order_key")
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


def test_bulk_add_last_key_read_failure_returns_upstream_error():
    db = _fake_add_db(last_key_error=APIError({"message": "connection refused"}))
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    db.rpc.assert_not_called()


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
    # The RPC's exception handler only recognizes a unique_violation on
    # ux_playlist_order_key (order_key_conflict); any other database error,
    # like this one, is re-raised and reaches the client library
    # unchanged, and nothing of it reaches the response.
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


def test_bulk_add_retries_with_fresh_keys_after_an_order_key_conflict():
    db = _fake_add_db(
        rpc_responses=[
            {"ok": False, "error": "order_key_conflict"},
            _bulk_rpc(added=1),
        ],
    )
    playlist_tracks = MagicMock()
    playlist_tracks.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.side_effect = [
        MagicMock(data=[{"order_key": "a1"}]),
        MagicMock(data=[{"order_key": "a2"}]),
    ]
    db.tables["playlist_tracks"] = playlist_tracks
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 200
    assert db.rpc.call_count == 2
    first_args, second_args = (call.args[1] for call in db.rpc.call_args_list)
    assert first_args["p_order_keys"] == ["a2"]
    assert second_args["p_order_keys"] == ["a3"]


def test_bulk_add_gives_up_with_conflict_after_three_order_key_conflicts():
    db = _fake_add_db(
        rpc_responses=[{"ok": False, "error": "order_key_conflict"}] * 3,
    )
    playlist_tracks = MagicMock()
    playlist_tracks.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.side_effect = [
        MagicMock(data=[{"order_key": f"a{index}"}]) for index in range(3)
    ]
    db.tables["playlist_tracks"] = playlist_tracks
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/tracks/bulk", json={"tracks": [_ADD_ONE_BODY]}
    )

    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "order_key_conflict"}
    assert db.rpc.call_count == 3
    assert (
        playlist_tracks.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.call_count
        == 3
    )


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
    # 1 -> 3 over three rows a0/a1/a2: o=0, t=2, so the window read is
    # range(1, 3) = [a1, a2], and since t > o the key lands after a2 -> a3.
    db = _fake_move_db(
        track_count=3,
        window_rows=[{"order_key": "a1"}, {"order_key": "a2"}],
    )
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
            "p_order_key": "a3",
        },
    )


def test_move_track_up_sends_a_key_before_the_destination():
    # 3 -> 1 over three rows a0/a1/a2: o=2, t=0, so the window read is
    # range(0, 1) and, since t < o, the key lands before the first row.
    db = _fake_move_db(track_count=3, window_rows=[{"order_key": "a0"}])
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 3, "new_position": 1},
    )

    assert response.status_code == 200
    assert db.rpc.call_args.args[1]["p_order_key"] == "Zz"
    playlist_tracks = db.tables["playlist_tracks"]
    playlist_tracks.select.return_value.eq.return_value.order.return_value.range.assert_called_once_with(
        0, 1
    )


def test_move_track_down_one_sends_a_key_between_the_next_two():
    # 1 -> 2 over three rows a0/a1/a2: o=0, t=1, so the window read is
    # range(0, 2), returning all three, and the key lands between a1 and
    # a2 -> a1V.
    db = _fake_move_db(
        track_count=3,
        window_rows=[{"order_key": "a0"}, {"order_key": "a1"}, {"order_key": "a2"}],
    )
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 200
    assert db.rpc.call_args.args[1]["p_order_key"] == "a1V"


def test_move_track_reads_the_neighbours_ordered_by_order_key():
    db = _fake_move_db(track_count=3, window_rows=[{"order_key": "a1"}])
    _use_db(db)
    _use_auth()

    client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    playlist_tracks = db.tables["playlist_tracks"]
    playlist_tracks.select.return_value.eq.return_value.order.assert_called_once_with(
        "order_key"
    )
    playlist_tracks.select.return_value.eq.return_value.order.return_value.range.assert_called_once_with(
        0, 2
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
    # The neighbour window is never read either: the request is rejected
    # before the retry loop it belongs to is reached.
    db.tables[
        "playlist_tracks"
    ].select.return_value.eq.return_value.order.assert_not_called()


def test_move_track_rejected_by_the_rpc_returns_upstream_error():
    # The RPC reports failure in its payload, not as an error status, so a
    # successful round trip can still mean it refused.
    db = _fake_move_db(rpc_data={"ok": False, "error": "playlist_locked"})
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    # Not retried: playlist_locked is not order_key_conflict.
    assert db.rpc.call_count == 1


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


def test_move_track_neighbour_read_failure_returns_upstream_error():
    db = _fake_move_db(window_error=APIError({"message": "connection refused"}))
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    db.rpc.assert_not_called()


def test_move_track_retries_with_a_fresh_key_after_an_order_key_conflict():
    db = _fake_move_db(
        rpc_responses=[
            {"ok": False, "error": "order_key_conflict"},
            {"ok": True, "order": [_TRACK_TWO_ID, _TRACK_ONE_ID]},
        ],
    )
    playlist_tracks = MagicMock()
    playlist_tracks.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[], count=2
    )
    # old_position=1, new_position=2 is o=0, t=1 (t > o): the key lands
    # after at(1), the second row of the window.
    playlist_tracks.select.return_value.eq.return_value.order.return_value.range.return_value.execute.side_effect = [
        MagicMock(data=[{"order_key": "a0"}, {"order_key": "a1"}]),
        MagicMock(data=[{"order_key": "a0"}, {"order_key": "a2"}]),
    ]
    db.tables["playlist_tracks"] = playlist_tracks
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 200
    assert db.rpc.call_count == 2
    first_args, second_args = (call.args[1] for call in db.rpc.call_args_list)
    assert first_args["p_order_key"] == "a2"
    assert second_args["p_order_key"] == "a3"


def test_move_track_gives_up_with_conflict_after_three_order_key_conflicts():
    db = _fake_move_db(
        rpc_responses=[{"ok": False, "error": "order_key_conflict"}] * 3,
    )
    playlist_tracks = MagicMock()
    playlist_tracks.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[], count=2
    )
    playlist_tracks.select.return_value.eq.return_value.order.return_value.range.return_value.execute.side_effect = [
        MagicMock(data=[{"order_key": f"a{index}"}]) for index in range(3)
    ]
    db.tables["playlist_tracks"] = playlist_tracks
    _use_db(db)
    _use_auth()

    response = client.post(
        f"/playlists/{_PLAYLIST_ID}/move-track",
        json={"old_position": 1, "new_position": 2},
    )

    assert response.status_code == 409
    assert response.json() == {"ok": False, "reason": "order_key_conflict"}
    assert db.rpc.call_count == 3
    assert (
        playlist_tracks.select.return_value.eq.return_value.order.return_value.range.return_value.execute.call_count
        == 3
    )


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
