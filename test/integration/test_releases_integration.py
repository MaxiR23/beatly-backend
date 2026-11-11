# test/integration/test_releases_integration.py
#
# Integration tests of POST /ingest/releases, GET /releases and of
# replace_new_releases (migration 053) against the local stack.
#
# Tested:
# - A valid submission answers 200 with the count, stores the rows with their
#   source, position 1..n and audio_playlist_id, and GET /releases serves them
#   in that order with the shape of the model (ties the literal of the source
#   between the SQL function and the service)
# - Sending the same body twice leaves the same rows; a new ingest replaces the
#   whole list
# - The rows of another source are neither deleted by the ingest nor served
# - The function is atomic: a repeated album or position (23505), a type, an
#   album id, an empty artists array, an http cover or a position 0 outside the
#   CHECKs (23514) fail and keep the previous list; an empty or non-array
#   payload (22023) fails and keeps it too
# - Two ingests at the same time both finish and the final state is exactly one
#   of the two lists
# - A rejected body (422) and a missing token (401) write nothing
# - GET /releases walks the pages in position order (total only in the first
#   page, a cursor, the last page without one) and an empty table is an empty
#   first page
# - anon and authenticated cannot execute the function nor read or write the
#   table (42501)
#
# What is covered:
# - The queries and the RPC of the routes and the migration 053 against the
#   real schema. The overlap of two ingests cannot be forced from PostgREST:
#   the test proves the result is right and that none fails
#
# Run with: pytest -m integration test/integration/test_releases_integration.py -v
#
# SEE: routes/ingest.py, routes/releases.py, services/release_service.py,
# db/migrations/053_replace_new_releases.sql

from concurrent.futures import ThreadPoolExecutor

import pytest
from postgrest.exceptions import APIError
from pydantic import SecretStr
from supabase import create_client

from core.config import settings
from core.database import get_ingest_db, get_user_client

pytestmark = pytest.mark.integration

_TOKEN = "a-fake-ingest-token-made-in-the-test-0123456789"
_SOURCE = "provider_new_releases"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "ingest_token", SecretStr(_TOKEN))


def _clear(admin):
    # The table is global: every test fixes its own state first.
    admin.table("new_releases").delete().eq("source", _SOURCE).execute()


def _release(n, **overrides):
    return {
        "album_id": f"MPREb_{n:011d}",
        "audio_playlist_id": f"OLAK5uy_{n:033d}",
        "type": "album",
        "title": f"Release {n}",
        "artists": [{"id": f"UC{n:022d}", "name": f"Artist {n}"}],
        "thumbnail_url": f"https://lh3.googleusercontent.com/rel{n}=w544-h544-l90-rj",
        **overrides,
    }


def _releases(count, start=1):
    return [_release(n) for n in range(start, start + count)]


def _numbered(releases):
    return [{**r, "position": p} for p, r in enumerate(releases, start=1)]


def _post(client, releases, token=_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/ingest/releases", json={"releases": releases}, headers=headers)


def _rpc(db, rows):
    return db.rpc("replace_new_releases", {"p_releases": rows}).execute()


def _stored(admin, source=_SOURCE):
    return (
        admin.table("new_releases")
        .select("*")
        .eq("source", source)
        .order("position")
        .execute()
        .data
    )


def _seed_previous(admin):
    _clear(admin)
    _rpc(admin, _numbered(_releases(3)))
    return _stored(admin)


# --- POST /ingest/releases and GET /releases ---------------------------------


def test_valid_ingest_is_stored_in_order_and_served_by_get_releases(client, admin):
    _clear(admin)
    sent = [_release(3), _release(1, type="single"), _release(2, type="ep")]

    response = _post(client, sent)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "data": {"releases": 3}}
    rows = _stored(admin)
    assert [(r["source"], r["position"]) for r in rows] == [
        (_SOURCE, 1),
        (_SOURCE, 2),
        (_SOURCE, 3),
    ]
    assert [r["audio_playlist_id"] for r in rows] == [
        r["audio_playlist_id"] for r in sent
    ]

    got = client.get("/releases")

    assert got.status_code == 200, got.text
    items = got.json()["data"]["items"]
    assert [i["album_id"] for i in items] == [r["album_id"] for r in sent]
    assert [i["type"] for i in items] == ["album", "single", "ep"]
    first = items[0]
    assert set(first) == {
        "album_id",
        "title",
        "type",
        "artists",
        "thumbnail_url",
        "image",
    }
    assert first["artists"] == sent[0]["artists"]
    assert first["thumbnail_url"] == (
        "https://lh3.googleusercontent.com/rel3=w544-h544-p-l90-rj"
    )
    assert first["image"]["small"].startswith("https://lh3.googleusercontent.com/rel3=")


def test_resending_the_same_body_leaves_the_same_rows(client, admin):
    _clear(admin)
    body = _releases(4)

    first = _post(client, body)
    before = _stored(admin)
    second = _post(client, body)

    assert first.json() == second.json() == {"ok": True, "data": {"releases": 4}}
    assert _stored(admin) == before


def test_a_new_ingest_replaces_the_whole_list(client, admin):
    _clear(admin)
    _post(client, _releases(3))

    response = _post(client, _releases(2, start=10))

    assert response.status_code == 200
    assert [r["album_id"] for r in _stored(admin)] == [
        _release(10)["album_id"],
        _release(11)["album_id"],
    ]


def test_rows_of_another_source_are_not_touched_and_not_served(client, admin):
    _clear(admin)
    other = {
        **_numbered([_release(50)])[0],
        "source": "other",
        "title": "From another source",
    }
    admin.table("new_releases").delete().eq("source", "other").execute()
    admin.table("new_releases").insert(other).execute()
    try:
        assert _post(client, _releases(2)).status_code == 200
        assert _stored(admin, "other") == [other]
        titles = [i["title"] for i in client.get("/releases").json()["data"]["items"]]
        assert titles == ["Release 1", "Release 2"]
    finally:
        admin.table("new_releases").delete().eq("source", "other").execute()


@pytest.mark.parametrize(
    ("name", "rows", "code"),
    [
        ("repeated_album", [_release(1), _release(1)], "23505"),
        ("type", [_release(1, type="song")], "23514"),
        ("album_id", [_release(1, album_id="XXXXb_1")], "23514"),
        ("empty_artists", [_release(1, artists=[])], "23514"),
        ("http_cover", [_release(1, thumbnail_url="http://example.com/c")], "23514"),
    ],
)
def test_rpc_failure_keeps_the_previous_list(admin, name, rows, code):
    before = _seed_previous(admin)

    with pytest.raises(APIError) as raised:
        _rpc(admin, _numbered(rows))

    assert raised.value.code == code, name
    assert _stored(admin) == before


def test_rpc_repeated_or_zero_position_keeps_the_previous_list(admin):
    before = _seed_previous(admin)
    pair = [{**_release(1), "position": 1}, {**_release(2), "position": 1}]
    zero = [{**_release(1), "position": 0}]

    with pytest.raises(APIError) as repeated:
        _rpc(admin, pair)
    with pytest.raises(APIError) as at_zero:
        _rpc(admin, zero)

    assert repeated.value.code == "23505"
    assert at_zero.value.code == "23514"
    assert _stored(admin) == before


@pytest.mark.parametrize("payload", [[], {}, "text", None])
def test_rpc_rejects_an_empty_or_non_array_payload_and_keeps_the_list(admin, payload):
    before = _seed_previous(admin)

    with pytest.raises(APIError) as raised:
        _rpc(admin, payload)

    assert raised.value.code == "22023"
    assert _stored(admin) == before


def test_two_concurrent_ingests_are_serialized(admin):
    _clear(admin)
    first = _numbered(_releases(200))
    second = _numbered(_releases(200, start=1000))
    clients = [admin, get_ingest_db()]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_rpc, db, rows) for db, rows in zip(clients, (first, second))
        ]
        results = [f.result() for f in futures]

    assert [r.data for r in results] == [200, 200]
    stored = [r["album_id"] for r in _stored(admin)]
    assert stored in (
        [r["album_id"] for r in first],
        [r["album_id"] for r in second],
    )


def test_rejected_body_and_missing_token_write_nothing(client, admin):
    before = _seed_previous(admin)

    rejected = _post(client, [_release(1, type="Album")])
    no_token = _post(client, _releases(2), token=None)

    assert rejected.status_code == 422
    assert rejected.json() == {"ok": False, "reason": "invalid_request"}
    assert no_token.status_code == 401
    assert _stored(admin) == before


def test_get_releases_walks_the_pages_in_position_order(client, admin):
    _clear(admin)
    _post(client, _releases(5))

    first = client.get("/releases?limit=2").json()["data"]
    second = client.get(
        f"/releases?limit=2&cursor={first['page']['next_cursor']}"
    ).json()["data"]
    last = client.get(
        f"/releases?limit=2&cursor={second['page']['next_cursor']}"
    ).json()["data"]

    assert [i["title"] for i in first["items"]] == ["Release 1", "Release 2"]
    assert first["page"]["total"] == 5
    assert first["page"]["has_more"] is True
    assert [i["title"] for i in second["items"]] == ["Release 3", "Release 4"]
    assert second["page"]["total"] is None
    assert [i["title"] for i in last["items"]] == ["Release 5"]
    assert last["page"]["has_more"] is False
    assert last["page"]["next_cursor"] is None
    assert all("position" not in i for i in first["items"])


def test_get_releases_with_no_rows_is_an_empty_first_page(client, admin):
    _clear(admin)

    response = client.get("/releases")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "items": [],
        "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 0},
    }


# --- Privileges --------------------------------------------------------------


def test_anon_and_authenticated_cannot_execute_the_function(make_user):
    clients = [
        create_client(settings.supabase_url, settings.supabase_anon_key),
        get_user_client(make_user().token),
    ]

    for db in clients:
        with pytest.raises(APIError) as called:
            _rpc(db, [])
        assert called.value.code == "42501"


def test_anon_and_authenticated_cannot_read_or_write_the_table(make_user):
    clients = [
        create_client(settings.supabase_url, settings.supabase_anon_key),
        get_user_client(make_user().token),
    ]

    for db in clients:
        with pytest.raises(APIError) as selected:
            db.table("new_releases").select("*").execute()
        with pytest.raises(APIError) as inserted:
            db.table("new_releases").insert(
                {**_numbered([_release(1)])[0], "source": _SOURCE}
            ).execute()
        assert selected.value.code == "42501"
        assert inserted.value.code == "42501"
