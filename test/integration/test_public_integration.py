# test/integration/test_public_integration.py
#
# Integration tests of the public share endpoints that read the database, against the local stack.
#
# Tested:
# - GET /public/playlists/{playlist_id} serves a public user playlist
#   with no token, with its tracks
# - GET /public/genre-playlists/{playlist_id} serves a genre playlist with
#   no token, with its tracks
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_public_integration.py -v
#
# SEE: routes/public.py, services/public_service.py

import pytest

pytestmark = pytest.mark.integration


def test_get_public_playlist_serves_a_public_playlist_without_a_token(
    client, make_user, track_payload
):
    user = make_user()
    created = client.post(
        "/playlists",
        json={"title": "Shared", "is_public": True},
        headers=user.headers,
    ).json()["data"]
    track = track_payload()
    client.post(f"/playlists/{created['id']}/tracks", json=track, headers=user.headers)

    response = client.get(f"/public/playlists/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["id"] == created["id"]
    assert body["data"]["title"] == "Shared"
    assert [t["track_id"] for t in body["data"]["tracks"]] == [track["track_id"]]


def test_get_public_genre_playlist_serves_a_genre_playlist_without_a_token(
    client, seed_genre_playlist
):
    seeded = seed_genre_playlist(track_count=2)

    response = client.get(f"/public/genre-playlists/{seeded['playlist']['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["title"] == seeded["playlist"]["title"]
    assert [t["track_id"] for t in body["data"]["tracks"]] == [
        t["track_id"] for t in seeded["tracks"]
    ]
