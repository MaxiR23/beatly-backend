# test/integration/test_library_integration.py
#
# Integration tests of the library endpoints against the local stack.
#
# Tested:
# - POST /library without optional fields stores the column defaults ('')
#   and answers null for them
# - POST /library with every field returns the 544 thumbnail and stores
#   the URL as sent
# - A re-POST that omits an optional, or sends it null, keeps the stored
#   value; one with new values replaces them. This lives here because a
#   mock cannot show that PostgREST leaves an omitted column alone
# - GET /library answers null for the empty fields of a saved item
# - A source outside library_items_source_check is 422 invalid_request
# - DELETE /library/{kind}/{external_id} removes the item
# - GET /library/{kind}/{external_id} reads saved true after POST, for
#   album and playlist, and false after DELETE
# - An item another user saved reads false for the caller
# - An item never saved reads false
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_library_integration.py -v
#
# SEE: routes/library.py, services/library_service.py, models/library.py

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

_OPTIONALS = ("thumbnail_url", "artist", "artist_id", "album_id", "album_name")
_THUMBNAIL_60 = "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj"
_THUMBNAIL_544 = "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"


def _required(external_id=None):
    return {
        "kind": "album",
        "external_id": external_id or f"it-{uuid4().hex}",
        "title": "Some Album",
        "source": "external",
    }


def _full(external_id=None):
    return {
        **_required(external_id),
        "thumbnail_url": _THUMBNAIL_60,
        "artist": "Some Artist",
        "artist_id": "artist-1",
        "album_id": "album-1",
        "album_name": "Some Album",
    }


def _row(admin, user, external_id):
    rows = (
        admin.table("library_items")
        .select("*")
        .eq("user_id", user.user_id)
        .eq("external_id", external_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


def test_post_library_without_optional_fields_stores_defaults_and_returns_nulls(
    client, make_user, admin
):
    user = make_user()
    body = _required()

    response = client.post("/library", json=body, headers=user.headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert response.json()["ok"] is True
    for column in _OPTIONALS:
        assert data[column] is None
    row = _row(admin, user, body["external_id"])
    for column in _OPTIONALS:
        assert row[column] == ""


def test_post_library_with_every_field_returns_the_square_thumbnail(
    client, make_user, admin
):
    user = make_user()
    body = _full()

    response = client.post("/library", json=body, headers=user.headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["thumbnail_url"] == _THUMBNAIL_544
    assert data["artist"] == "Some Artist"
    assert _row(admin, user, body["external_id"])["thumbnail_url"] == _THUMBNAIL_60


def test_re_post_library_without_optional_fields_keeps_the_stored_values(
    client, make_user, admin
):
    user = make_user()
    body = _full()
    client.post("/library", json=body, headers=user.headers)

    response = client.post(
        "/library",
        json={**_required(body["external_id"]), "title": "Renamed"},
        headers=user.headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["title"] == "Renamed"
    assert data["artist"] == "Some Artist"
    row = _row(admin, user, body["external_id"])
    assert row["title"] == "Renamed"
    for column in _OPTIONALS:
        assert row[column] == body[column]


def test_re_post_library_with_an_explicit_null_keeps_the_stored_value(
    client, make_user, admin
):
    user = make_user()
    body = _full()
    client.post("/library", json=body, headers=user.headers)

    response = client.post(
        "/library", json={**body, "artist": None}, headers=user.headers
    )

    assert response.status_code == 200
    assert response.json()["data"]["artist"] == "Some Artist"
    assert _row(admin, user, body["external_id"])["artist"] == "Some Artist"


def test_re_post_library_with_new_values_replaces_them(client, make_user, admin):
    user = make_user()
    body = _full()
    first = client.post("/library", json=body, headers=user.headers).json()["data"]
    replacement = {
        **body,
        "thumbnail_url": "https://example.com/new.png",
        "artist": "New Artist",
        "artist_id": "artist-2",
        "album_id": "album-2",
        "album_name": "New Album",
    }

    response = client.post("/library", json=replacement, headers=user.headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artist"] == "New Artist"
    assert data["added_at"] == first["added_at"]
    row = _row(admin, user, body["external_id"])
    for column in _OPTIONALS:
        assert row[column] == replacement[column]


def test_get_library_returns_null_for_the_empty_fields_of_a_saved_item(
    client, make_user
):
    user = make_user()
    body = _required()
    client.post("/library", json=body, headers=user.headers)

    response = client.get("/library", headers=user.headers)

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    [entry] = [item for item in items if item["id"] == body["external_id"]]
    assert entry["thumbnail_url"] is None
    assert entry["subtitle"] is None


def test_post_library_with_a_source_outside_the_check_is_invalid_request(
    client, make_user, admin
):
    user = make_user()
    body = {**_full(), "source": "spotify"}

    response = client.post("/library", json=body, headers=user.headers)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert _row(admin, user, body["external_id"]) is None


def test_delete_library_item_removes_it(client, make_user, admin):
    user = make_user()
    body = _required()
    client.post("/library", json=body, headers=user.headers)

    response = client.delete(
        f"/library/album/{body['external_id']}", headers=user.headers
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    assert _row(admin, user, body["external_id"]) is None


@pytest.mark.parametrize("kind", ["album", "playlist"])
def test_get_library_item_reads_saved_after_post(client, make_user, kind):
    user = make_user()
    body = {**_required(), "kind": kind}
    client.post("/library", json=body, headers=user.headers)

    response = client.get(
        f"/library/{kind}/{body['external_id']}", headers=user.headers
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"saved": True}}


def test_get_library_item_reads_not_saved_after_delete(client, make_user):
    user = make_user()
    body = _required()
    client.post("/library", json=body, headers=user.headers)
    client.delete(f"/library/album/{body['external_id']}", headers=user.headers)

    response = client.get(f"/library/album/{body['external_id']}", headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"saved": False}}


def test_get_library_item_never_saved_reads_false(client, make_user):
    user = make_user()

    response = client.get(f"/library/album/it-{uuid4().hex}", headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"saved": False}}


def test_get_library_item_saved_by_another_user_reads_false(client, make_user):
    owner = make_user()
    other = make_user()
    body = _required()
    client.post("/library", json=body, headers=owner.headers)
    path = f"/library/album/{body['external_id']}"

    other_response = client.get(path, headers=other.headers)
    owner_response = client.get(path, headers=owner.headers)

    assert other_response.json() == {"ok": True, "data": {"saved": False}}
    assert owner_response.json() == {"ok": True, "data": {"saved": True}}
