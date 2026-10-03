# test/integration/test_likes_integration.py
#
# Integration tests of the likes endpoints against the local Supabase stack.
#
# Tested:
# - GET /likes reads the track fields from the catalog through the like's
#   relation, with the thumbnail rewritten to 544
# - An empty first page, and pagination over the embed
# - GET /likes/sync returns active and unliked likes with the catalog
#   fields; an empty page when nothing changed; pagination over the embed
# - POST /likes writes the track to the catalog and the like, refreshes
#   the metadata of a known track, keeps the catalog duration when none is
#   sent, is 422 invalid_request when none is sent for an unknown track,
#   and revives an unliked track
# - DELETE /likes/{track_id} soft-deletes and moves updated_at; unliking a
#   track that is not liked is still 200
#
# - The likes select (likes_service._COLUMNS) returns the catalog track
#   spread into the row: no "tracks" key, exactly the fields of Like
#
# What is covered:
# - The queries, the embed and the upserts against the real schema, with
#   RLS running as the caller
#
# Run with: pytest -m integration test/integration/test_likes_integration.py -v
#
# SEE: routes/likes.py, services/likes_service.py, test/routes/test_likes.py

import pytest

from core.database import get_user_client
from models.likes import Like
from services import likes_service

pytestmark = pytest.mark.integration

_FAR_PAST = "2000-01-01T00:00:00Z"
_THUMBNAIL_544 = "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"


def _like(client, user, track):
    response = client.post("/likes", json=track, headers=user.headers)
    assert response.status_code == 200
    return response.json()["data"]


def _catalog_row(admin, track_id):
    return admin.table("tracks").select("*").eq("track_id", track_id).execute().data


def _like_row(admin, user, track_id):
    return (
        admin.table("user_likes")
        .select("*")
        .eq("user_id", user.user_id)
        .eq("track_id", track_id)
        .execute()
        .data
    )


def test_get_likes_returns_the_catalog_fields_of_a_liked_track(
    client, make_user, track_payload, admin
):
    user = make_user()
    track = track_payload()
    _like(client, user, track)

    response = client.get("/likes", headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    [item] = body["data"]["items"]
    stored = _catalog_row(admin, track["track_id"])[0]
    assert item["track_id"] == track["track_id"]
    for field in ("title", "artists", "album", "album_id", "duration_seconds"):
        assert item[field] == stored[field]
    assert item["thumbnail_url"] == _THUMBNAIL_544
    assert item["deleted_at"] is None


def test_the_likes_select_spreads_the_catalog_track_into_the_row(
    client, make_user, track_payload
):
    user = make_user()
    track = track_payload()
    _like(client, user, track)

    [row] = (
        get_user_client(user.token)
        .table("user_likes")
        .select(likes_service._COLUMNS)
        .eq("track_id", track["track_id"])
        .execute()
        .data
    )

    assert "tracks" not in row
    assert set(row) == set(Like.model_fields)


def test_get_likes_without_active_likes_is_an_empty_first_page(client, make_user):
    user = make_user()

    response = client.get("/likes", headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [],
            "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 0},
        },
    }


def test_get_likes_paginates_over_the_embed(client, make_user, track_payload):
    user = make_user()
    tracks = [track_payload() for _ in range(3)]
    for track in tracks:
        _like(client, user, track)

    seen = []
    cursor = None
    pages = []
    for _ in range(3):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/likes", params=params, headers=user.headers)
        assert response.status_code == 200
        data = response.json()["data"]
        pages.append(data["page"])
        seen.extend(item["track_id"] for item in data["items"])
        assert data["items"][0]["title"] == "Integration Track"
        cursor = data["page"]["next_cursor"]

    assert seen == [track["track_id"] for track in tracks]
    assert pages[0]["has_more"] is True
    assert pages[0]["total"] == 3
    assert pages[1]["total"] is None
    assert pages[1]["has_more"] is True
    assert pages[2]["has_more"] is False
    assert pages[2]["next_cursor"] is None


def test_sync_returns_active_and_unliked_likes_with_catalog_fields(
    client, make_user, track_payload
):
    user = make_user()
    kept, dropped = track_payload(), track_payload()
    _like(client, user, kept)
    _like(client, user, dropped)
    client.delete(f"/likes/{dropped['track_id']}", headers=user.headers)

    response = client.get(
        "/likes/sync", params={"since": _FAR_PAST}, headers=user.headers
    )

    assert response.status_code == 200
    items = {i["track_id"]: i for i in response.json()["data"]["items"]}
    assert set(items) == {kept["track_id"], dropped["track_id"]}
    assert items[kept["track_id"]]["deleted_at"] is None
    assert items[dropped["track_id"]]["deleted_at"] is not None
    for track in (kept, dropped):
        item = items[track["track_id"]]
        assert item["title"] == track["title"]
        assert item["artists"] == track["artists"]
        assert item["thumbnail_url"] == _THUMBNAIL_544


def test_sync_without_changes_since_is_an_empty_page(client, make_user, track_payload):
    user = make_user()
    _like(client, user, track_payload())
    first = client.get("/likes/sync", params={"since": _FAR_PAST}, headers=user.headers)
    # A timestamp the database produced, not the host clock: the VM clock
    # of Docker can be minutes off.
    latest = max(item["updated_at"] for item in first.json()["data"]["items"])

    response = client.get("/likes/sync", params={"since": latest}, headers=user.headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["items"] == []
    assert data["page"]["has_more"] is False


def test_sync_paginates_over_the_embed(client, make_user, track_payload):
    user = make_user()
    for _ in range(3):
        _like(client, user, track_payload())

    pages = []
    seen = []
    cursor = None
    for index in range(3):
        params = {"limit": 1}
        if index == 0:
            params["since"] = _FAR_PAST
        else:
            params["cursor"] = cursor
        response = client.get("/likes/sync", params=params, headers=user.headers)
        assert response.status_code == 200
        data = response.json()["data"]
        pages.append(data["page"])
        seen.extend(item["track_id"] for item in data["items"])
        assert data["items"][0]["title"] == "Integration Track"
        cursor = data["page"]["next_cursor"]

    assert len(set(seen)) == 3
    assert pages[0]["has_more"] is True
    assert pages[0]["total"] == 3
    assert pages[1]["total"] is None
    assert pages[2]["next_cursor"] is None


def test_post_like_writes_the_track_to_the_catalog_and_the_like(
    client, make_user, track_payload, admin
):
    user = make_user()
    track = track_payload()

    response = client.post("/likes", json=track, headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    like = body["data"]
    assert set(like) == {
        "track_id",
        "title",
        "artists",
        "album",
        "album_id",
        "thumbnail_url",
        "duration_seconds",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert like["title"] == track["title"]
    assert like["thumbnail_url"] == _THUMBNAIL_544
    [stored] = _catalog_row(admin, track["track_id"])
    for field, value in track.items():
        assert stored[field] == value
    [row] = _like_row(admin, user, track["track_id"])
    assert row["deleted_at"] is None


def test_post_like_refreshes_the_catalog_metadata_of_a_known_track(
    client, make_user, seed_tracks, admin
):
    user = make_user()
    [known] = seed_tracks(1)

    response = client.post(
        "/likes", json={**known, "title": "Renamed"}, headers=user.headers
    )

    assert response.status_code == 200
    assert response.json()["data"]["title"] == "Renamed"
    [stored] = _catalog_row(admin, known["track_id"])
    assert stored["title"] == "Renamed"


def test_post_like_without_duration_keeps_the_catalog_duration(
    client, make_user, seed_tracks, admin
):
    user = make_user()
    [known] = seed_tracks(1)
    body = {k: v for k, v in known.items() if k != "duration_seconds"}

    response = client.post(
        "/likes", json={**body, "title": "Renamed"}, headers=user.headers
    )

    assert response.status_code == 200
    assert response.json()["data"]["duration_seconds"] == known["duration_seconds"]
    [stored] = _catalog_row(admin, known["track_id"])
    assert stored["duration_seconds"] == known["duration_seconds"]
    assert stored["title"] == "Renamed"


def test_post_like_without_duration_for_an_unknown_track_is_invalid_request(
    client, make_user, track_payload, admin
):
    user = make_user()
    track = track_payload()
    body = {k: v for k, v in track.items() if k != "duration_seconds"}

    response = client.post("/likes", json=body, headers=user.headers)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert _catalog_row(admin, track["track_id"]) == []
    assert _like_row(admin, user, track["track_id"]) == []


def test_post_like_revives_an_unliked_track(client, make_user, track_payload, admin):
    user = make_user()
    track = track_payload()
    first = _like(client, user, track)
    client.delete(f"/likes/{track['track_id']}", headers=user.headers)

    response = client.post("/likes", json=track, headers=user.headers)

    assert response.status_code == 200
    revived = response.json()["data"]
    assert revived["deleted_at"] is None
    assert revived["created_at"] == first["created_at"]
    [row] = _like_row(admin, user, track["track_id"])
    assert row["deleted_at"] is None
    assert row["created_at"] == first["created_at"]


def test_delete_like_soft_deletes_and_moves_updated_at(
    client, make_user, track_payload, admin
):
    user = make_user()
    track = track_payload()
    liked = _like(client, user, track)

    response = client.delete(f"/likes/{track['track_id']}", headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    [row] = _like_row(admin, user, track["track_id"])
    assert row["deleted_at"] is not None
    assert row["updated_at"] != liked["updated_at"]


def test_delete_like_of_a_track_not_liked_is_ok(client, make_user):
    user = make_user()

    response = client.delete("/likes/it-never-liked", headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
