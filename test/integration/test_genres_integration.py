# test/integration/test_genres_integration.py
#
# Integration tests of the genres endpoints against the local stack.
#
# Tested:
# - GET /genres lists the seeded genre
# - GET /genres/{slug}/playlists lists the seeded playlist
# - GET /genres/{slug}/categories lists the seeded category
# - GET /genre-playlists/{playlist_id}/tracks lists the seeded tracks in
#   position order
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_genres_integration.py -v
#
# SEE: routes/genres.py, services/genre_service.py

import pytest

pytestmark = pytest.mark.integration


def test_get_genres_includes_the_seeded_genre(client, make_user, seed_genre_playlist):
    user = make_user()
    seeded = seed_genre_playlist()

    response = client.get("/genres", headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    slugs = [item["slug"] for item in body["data"]["items"]]
    assert seeded["genre"]["slug"] in slugs


def test_get_genre_playlists_lists_the_seeded_playlist(
    client, make_user, seed_genre_playlist
):
    user = make_user()
    seeded = seed_genre_playlist()

    response = client.get(
        f"/genres/{seeded['genre']['slug']}/playlists", headers=user.headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    [item] = body["data"]["items"]
    assert item["id"] == seeded["playlist"]["id"]
    assert item["title"] == seeded["playlist"]["title"]


def test_get_genre_categories_lists_the_seeded_category(
    client, make_user, seed_genre_playlist
):
    user = make_user()
    seeded = seed_genre_playlist()

    response = client.get(
        f"/genres/{seeded['genre']['slug']}/categories", headers=user.headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["items"] == [seeded["playlist"]["category"]]


def test_get_genre_playlist_tracks_lists_the_seeded_tracks_in_order(
    client, make_user, seed_genre_playlist
):
    user = make_user()
    seeded = seed_genre_playlist(track_count=3)

    response = client.get(
        f"/genre-playlists/{seeded['playlist']['id']}/tracks", headers=user.headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    items = body["data"]["items"]
    assert [item["track_id"] for item in items] == [
        track["track_id"] for track in seeded["tracks"]
    ]
    assert [item["position"] for item in items] == [1, 2, 3]
