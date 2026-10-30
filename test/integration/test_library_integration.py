# test/integration/test_library_integration.py
#
# Integration tests of the library endpoints against the local stack.
#
# Tested:
# - POST /library without optional fields stores the column defaults ('')
#   and answers null for them
# - POST /library with every field returns the 544 thumbnail and its image
#   tiers, and stores the URL as sent
# - GET /library gives image null and mosaic [] to a saved item without
#   thumbnail, and an own playlist carries one mosaic tier set per URL
# - A re-POST that omits an optional, or sends it null, keeps the stored
#   value; one with new values replaces them. This lives here because a
#   mock cannot show that PostgREST leaves an omitted column alone
# - GET /library answers null for the empty fields of a saved item
# - A source outside library_items_source_check is 422 invalid_request
# - DELETE /library/{kind}/{external_id} removes the item
# - GET /library/{kind}/{external_id} reads saved true after POST, for
#   album, playlist and artist, and false after DELETE
# - An item another user saved reads false for the caller, for album and
#   artist (#219)
# - An item never saved reads false
# - kind "artist" (#219): POST stores it with the square thumbnail and
#   re-POST keeps one row, saved state and DELETE round trip, GET /library
#   lists it, a kind outside the model is 422, and the CHECK of 046 admits
#   artist and still refuses a kind outside it (written with the service
#   role, which skips the model)
# - GET /library?kind= narrows to one kind (no liked entry, total of that
#   kind), walks across pages, answers the general empty shape, and 422
#   invalid_cursor for a cursor from another filter
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
from postgrest.exceptions import APIError

pytestmark = pytest.mark.integration

_OPTIONALS = ("thumbnail_url", "artist", "artist_id", "album_id", "album_name")
_THUMBNAIL_60 = "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj"
_THUMBNAIL_544 = "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"
_IMAGE = {
    "small": "https://lh3.googleusercontent.com/abc=w192-h192-p-l90-rj",
    "medium": "https://lh3.googleusercontent.com/abc=w600-h600-p-l90-rj",
    "large": "https://lh3.googleusercontent.com/abc=w1320-h1320-p-l90-rj",
    "color": None,
}


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


def _artist(external_id=None):
    return {
        "kind": "artist",
        "external_id": external_id or f"it-{uuid4().hex}",
        "title": "Some Singer",
        "source": "external",
        "thumbnail_url": "https://lh3.googleusercontent.com/abc=w1200-h1200-p-l90-rj",
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
    assert data["image"] == _IMAGE
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
    assert entry["image"] is None
    assert entry["mosaic"] == []
    assert entry["subtitle"] is None


def test_get_library_own_playlist_carries_mosaic_tiers(
    client, make_user, track_payload
):
    user = make_user()
    playlist = client.post(
        "/playlists", json={"title": "Mine"}, headers=user.headers
    ).json()["data"]
    client.post(
        f"/playlists/{playlist['id']}/tracks",
        json=track_payload(),
        headers=user.headers,
    )

    response = client.get("/library", headers=user.headers)

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    [entry] = [item for item in items if item["id"] == playlist["id"]]
    assert entry["thumbnail_urls"] == [_THUMBNAIL_544]
    assert entry["mosaic"] == [_IMAGE]


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


@pytest.mark.parametrize("kind", ["album", "playlist", "artist"])
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


@pytest.mark.parametrize("kind", ["album", "artist"])
def test_get_library_item_saved_by_another_user_reads_false(client, make_user, kind):
    owner = make_user()
    other = make_user()
    body = {**_required(), "kind": kind}
    client.post("/library", json=body, headers=owner.headers)
    path = f"/library/{kind}/{body['external_id']}"

    other_response = client.get(path, headers=other.headers)
    owner_response = client.get(path, headers=owner.headers)

    assert other_response.json() == {"ok": True, "data": {"saved": False}}
    assert owner_response.json() == {"ok": True, "data": {"saved": True}}


# --- Artists (#219) --------------------------------------------------------


def test_post_library_artist_is_stored_and_returns_the_square_thumbnail(
    client, make_user, admin
):
    user = make_user()
    body = _artist()

    response = client.post("/library", json=body, headers=user.headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["kind"] == "artist"
    assert data["thumbnail_url"] == _THUMBNAIL_544
    assert data["image"] == _IMAGE
    row = _row(admin, user, body["external_id"])
    assert row["kind"] == "artist"
    assert row["thumbnail_url"] == body["thumbnail_url"]


def test_re_post_library_artist_keeps_a_single_row(client, make_user, admin):
    user = make_user()
    body = _artist()

    first = client.post("/library", json=body, headers=user.headers)
    second = client.post("/library", json=body, headers=user.headers)

    assert first.status_code == 200
    assert second.status_code == 200
    count = (
        admin.table("library_items")
        .select("id", count="exact")
        .eq("user_id", user.user_id)
        .eq("kind", "artist")
        .eq("external_id", body["external_id"])
        .execute()
        .count
    )
    assert count == 1


def test_artist_saved_state_and_delete_round_trip(client, make_user, admin):
    user = make_user()
    body = _artist()
    path = f"/library/artist/{body['external_id']}"
    client.post("/library", json=body, headers=user.headers)

    saved = client.get(path, headers=user.headers)
    removed = client.delete(path, headers=user.headers)
    after = client.get(path, headers=user.headers)
    again = client.delete(path, headers=user.headers)

    assert saved.json() == {"ok": True, "data": {"saved": True}}
    assert removed.status_code == 200
    assert removed.json() == {"ok": True, "data": None}
    assert _row(admin, user, body["external_id"]) is None
    assert after.json() == {"ok": True, "data": {"saved": False}}
    assert again.status_code == 404
    assert again.json() == {"ok": False, "reason": "library_item_not_found"}


def test_get_library_includes_a_saved_artist(client, make_user):
    user = make_user()
    album = _required()
    artist = _artist()
    bare = {k: v for k, v in _artist().items() if k != "thumbnail_url"}
    client.post("/library", json=album, headers=user.headers)
    client.post("/library", json=artist, headers=user.headers)
    client.post("/library", json=bare, headers=user.headers)

    response = client.get("/library", headers=user.headers)

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    ids = [item["id"] for item in items]
    assert ids == [
        "liked",
        bare["external_id"],
        artist["external_id"],
        album["external_id"],
    ]
    assert items[2] == {
        "kind": "artist",
        "id": artist["external_id"],
        "title": "Some Singer",
        "thumbnail_url": _THUMBNAIL_544,
        "subtitle": None,
        "source": "external",
        "thumbnail_urls": [],
        "image": _IMAGE,
        "mosaic": [],
    }
    assert items[1]["thumbnail_url"] is None
    assert items[1]["image"] is None


def test_post_library_with_a_kind_outside_the_check_is_invalid_request(
    client, make_user, admin
):
    user = make_user()
    body = {**_artist(), "kind": "song"}

    response = client.post("/library", json=body, headers=user.headers)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert _row(admin, user, body["external_id"]) is None


def test_library_items_check_rejects_a_kind_outside_it(make_user, admin):
    user = make_user()
    row = {
        "user_id": user.user_id,
        "title": "t",
        "source": "external",
    }

    with pytest.raises(APIError) as excinfo:
        admin.table("library_items").insert(
            {**row, "kind": "song", "external_id": "check-song"}
        ).execute()
    assert excinfo.value.code == "23514"

    inserted = (
        admin.table("library_items")
        .insert({**row, "kind": "artist", "external_id": "check-artist"})
        .execute()
    )
    assert inserted.data[0]["kind"] == "artist"


def _seed_mixed_library(client, user):
    own = client.post(
        "/playlists", json={"title": "Mine"}, headers=user.headers
    ).json()["data"]
    album = _required()
    saved_playlist = {**_required(), "kind": "playlist"}
    artists = [_artist(), _artist()]
    for body in (album, saved_playlist, *artists):
        assert (
            client.post("/library", json=body, headers=user.headers).status_code == 200
        )
    return own, album, saved_playlist, artists


def test_get_library_filtered_by_kind_returns_only_that_kind(client, make_user):
    user = make_user()
    own, album, saved_playlist, artists = _seed_mixed_library(client, user)

    def ids(**params):
        response = client.get("/library", params=params, headers=user.headers)
        assert response.status_code == 200
        data = response.json()["data"]
        return [item["id"] for item in data["items"]], data["page"]["total"]

    artist_ids, artist_total = ids(kind="artist")
    assert sorted(artist_ids) == sorted(a["external_id"] for a in artists)
    assert artist_total == 2

    playlist_ids, playlist_total = ids(kind="playlist")
    assert sorted(playlist_ids) == sorted([own["id"], saved_playlist["external_id"]])
    assert playlist_total == 2

    assert ids(kind="album") == ([album["external_id"]], 1)

    all_ids, all_total = ids()
    assert all_ids[0] == "liked"
    assert all_total == 5


def test_get_library_filtered_walks_across_pages(client, make_user):
    user = make_user()
    _, _, _, artists = _seed_mixed_library(client, user)
    seen = []
    totals = []
    params = {"kind": "artist", "limit": 1}

    while True:
        response = client.get("/library", params=params, headers=user.headers)
        assert response.status_code == 200
        data = response.json()["data"]
        seen += [item["id"] for item in data["items"]]
        totals.append(data["page"]["total"])
        cursor = data["page"]["next_cursor"]
        if cursor is None:
            break
        params = {"kind": "artist", "limit": 1, "cursor": cursor}

    assert sorted(seen) == sorted(a["external_id"] for a in artists)
    assert len(seen) == 2
    assert totals == [2, None]


def test_get_library_rejects_a_cursor_from_another_kind_filter(client, make_user):
    user = make_user()
    _seed_mixed_library(client, user)
    artist_page = client.get(
        "/library", params={"kind": "artist", "limit": 1}, headers=user.headers
    )
    plain_page = client.get("/library", params={"limit": 1}, headers=user.headers)
    artist_cursor = artist_page.json()["data"]["page"]["next_cursor"]
    plain_cursor = plain_page.json()["data"]["page"]["next_cursor"]
    assert artist_cursor is not None
    assert plain_cursor is not None

    for params in (
        {"kind": "album", "cursor": artist_cursor},
        {"cursor": artist_cursor},
        {"kind": "artist", "cursor": plain_cursor},
    ):
        response = client.get("/library", params=params, headers=user.headers)

        assert response.status_code == 422
        assert response.json() == {"ok": False, "reason": "invalid_cursor"}


def test_get_library_filtered_by_kind_empty_is_an_empty_first_page(client, make_user):
    user = make_user()
    client.post("/library", json=_required(), headers=user.headers)

    response = client.get("/library", params={"kind": "artist"}, headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [],
            "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 0},
        },
    }
