# test/integration/test_playlists_integration.py
#
# Integration tests of the playlists endpoints against the local stack.
#
# Tested:
# - POST /playlists creates a playlist owned by the caller
# - GET /playlists lists the caller's playlists
# - GET /playlists/{id}, /tracks and /track-ids read a playlist with tracks
# - GET /playlists/owned-with-track/{track_id} lists the playlists that
#   hold the track
# - GET /playlists/liked, /liked/tracks and /liked/track-ids read the
#   virtual liked playlist
# - PATCH and DELETE /playlists/{id} update and remove a playlist
# - POST /playlists/{id}/tracks and /tracks/bulk add tracks, writing the
#   catalog
# - DELETE /playlists/{id}/tracks/{track_id} removes a track
# - POST /playlists/{id}/move-track reorders the tracks
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_playlists_integration.py -v
#
# SEE: routes/playlists.py, services/playlist_service.py

import pytest

pytestmark = pytest.mark.integration


def _create(client, user, **fields):
    response = client.post(
        "/playlists", json={"title": "Mix", **fields}, headers=user.headers
    )
    assert response.status_code == 200
    return response.json()["data"]


def _add(client, user, playlist_id, track):
    response = client.post(
        f"/playlists/{playlist_id}/tracks", json=track, headers=user.headers
    )
    assert response.status_code == 200
    return response.json()["data"]


def _playlist_with_tracks(client, user, track_payload, count=2):
    playlist = _create(client, user)
    tracks = [track_payload() for _ in range(count)]
    for track in tracks:
        _add(client, user, playlist["id"], track)
    return playlist, tracks


def test_post_playlist_creates_a_playlist_owned_by_the_caller(client, make_user, admin):
    user = make_user()

    response = client.post(
        "/playlists",
        json={"title": "Road trip", "description": "Songs", "is_public": True},
        headers=user.headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["title"] == "Road trip"
    assert body["data"]["owner_id"] == user.user_id
    stored = (
        admin.table("playlists")
        .select("title, is_public")
        .eq("id", body["data"]["id"])
        .execute()
        .data
    )
    assert stored == [{"title": "Road trip", "is_public": True}]


def test_get_playlists_lists_the_callers_playlists(client, make_user):
    user = make_user()
    playlist = _create(client, user, title="Listed")

    response = client.get("/playlists", headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    [item] = body["data"]["items"]
    assert item["id"] == playlist["id"]
    assert item["title"] == "Listed"
    assert item["thumbnail_urls"] == []


def test_get_playlist_returns_the_detail_with_totals(client, make_user, track_payload):
    user = make_user()
    playlist, tracks = _playlist_with_tracks(client, user, track_payload, count=2)

    response = client.get(f"/playlists/{playlist['id']}", headers=user.headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == playlist["id"]
    assert data["total_count"] == 2
    assert data["total_duration_seconds"] == sum(t["duration_seconds"] for t in tracks)


def test_get_playlist_tracks_lists_the_tracks_in_order(
    client, make_user, track_payload
):
    user = make_user()
    playlist, tracks = _playlist_with_tracks(client, user, track_payload, count=3)

    response = client.get(f"/playlists/{playlist['id']}/tracks", headers=user.headers)

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["track_id"] for item in items] == [t["track_id"] for t in tracks]
    assert [item["position"] for item in items] == [1, 2, 3]
    assert "id" not in items[0]


def test_get_playlist_track_ids_lists_the_track_ids_in_order(
    client, make_user, track_payload
):
    user = make_user()
    playlist, tracks = _playlist_with_tracks(client, user, track_payload, count=2)

    response = client.get(
        f"/playlists/{playlist['id']}/track-ids", headers=user.headers
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"] == [t["track_id"] for t in tracks]


def test_get_owned_playlists_with_track_lists_the_playlists_holding_it(
    client, make_user, track_payload
):
    user = make_user()
    playlist, tracks = _playlist_with_tracks(client, user, track_payload, count=1)
    _create(client, user, title="Without it")

    response = client.get(
        f"/playlists/owned-with-track/{tracks[0]['track_id']}", headers=user.headers
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"playlist_ids": [playlist["id"]]}


def _like_two(client, user, track_payload):
    tracks = [track_payload() for _ in range(2)]
    for track in tracks:
        assert (
            client.post("/likes", json=track, headers=user.headers).status_code == 200
        )
    return tracks


def test_get_liked_playlist_returns_the_virtual_playlist_with_totals(
    client, make_user, track_payload
):
    user = make_user()
    tracks = _like_two(client, user, track_payload)

    response = client.get("/playlists/liked", headers=user.headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == "liked"
    assert data["total_count"] == 2
    assert data["total_duration_seconds"] == sum(t["duration_seconds"] for t in tracks)


def test_get_liked_playlist_tracks_lists_the_liked_tracks(
    client, make_user, track_payload
):
    user = make_user()
    tracks = _like_two(client, user, track_payload)

    response = client.get("/playlists/liked/tracks", headers=user.headers)

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert sorted(item["track_id"] for item in items) == sorted(
        t["track_id"] for t in tracks
    )
    assert all(item["title"] == "Integration Track" for item in items)


def test_get_liked_playlist_track_ids_lists_the_liked_track_ids(
    client, make_user, track_payload
):
    user = make_user()
    tracks = _like_two(client, user, track_payload)

    response = client.get("/playlists/liked/track-ids", headers=user.headers)

    assert response.status_code == 200
    assert sorted(response.json()["data"]["items"]) == sorted(
        t["track_id"] for t in tracks
    )


def test_patch_playlist_updates_the_title(client, make_user, admin):
    user = make_user()
    playlist = _create(client, user)

    response = client.patch(
        f"/playlists/{playlist['id']}",
        json={"title": "Renamed"},
        headers=user.headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["title"] == "Renamed"
    stored = admin.table("playlists").select("title").eq("id", playlist["id"])
    assert stored.execute().data == [{"title": "Renamed"}]


def test_delete_playlist_removes_it(client, make_user, admin):
    user = make_user()
    playlist = _create(client, user)

    response = client.delete(f"/playlists/{playlist['id']}", headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    stored = admin.table("playlists").select("id").eq("id", playlist["id"])
    assert stored.execute().data == []


def test_post_playlist_track_adds_it_and_writes_the_catalog(
    client, make_user, track_payload, admin
):
    user = make_user()
    playlist = _create(client, user)
    track = track_payload()

    response = client.post(
        f"/playlists/{playlist['id']}/tracks", json=track, headers=user.headers
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["track_id"] == track["track_id"]
    assert data["position"] == 1
    stored = admin.table("tracks").select("title").eq("track_id", track["track_id"])
    assert stored.execute().data == [{"title": track["title"]}]


def test_post_playlist_tracks_bulk_adds_the_batch(client, make_user, track_payload):
    user = make_user()
    playlist = _create(client, user)
    tracks = [track_payload() for _ in range(3)]

    response = client.post(
        f"/playlists/{playlist['id']}/tracks/bulk",
        json={"tracks": tracks},
        headers=user.headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"added": 3, "skipped": 0}
    listed = client.get(f"/playlists/{playlist['id']}/track-ids", headers=user.headers)
    assert listed.json()["data"]["items"] == [t["track_id"] for t in tracks]


def test_delete_playlist_track_removes_it(client, make_user, track_payload):
    user = make_user()
    playlist, tracks = _playlist_with_tracks(client, user, track_payload, count=2)

    response = client.delete(
        f"/playlists/{playlist['id']}/tracks/{tracks[0]['track_id']}",
        headers=user.headers,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    listed = client.get(f"/playlists/{playlist['id']}/track-ids", headers=user.headers)
    assert listed.json()["data"]["items"] == [tracks[1]["track_id"]]


def test_post_move_track_reorders_the_tracks(client, make_user, track_payload):
    user = make_user()
    playlist, tracks = _playlist_with_tracks(client, user, track_payload, count=3)

    response = client.post(
        f"/playlists/{playlist['id']}/move-track",
        json={"old_position": 1, "new_position": 3},
        headers=user.headers,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    listed = client.get(f"/playlists/{playlist['id']}/track-ids", headers=user.headers)
    assert listed.json()["data"]["items"] == [
        tracks[1]["track_id"],
        tracks[2]["track_id"],
        tracks[0]["track_id"],
    ]
