# test/integration/test_genres_integration.py
#
# Integration tests of the genres endpoints against the local stack.
#
# Tested:
# - GET /genres lists the seeded genre
# - GET /genres/{slug}/playlists lists the seeded playlist
# - GET /genres/{slug}/playlists with two playlists reads each one's own
#   mosaic through the get_playlist_thumbnails RPC over GET with a two-id
#   array literal
# - GET /genres/{slug}/playlists carries mosaic tiers (one per thumbnail_urls
#   entry) and GET /genre-playlists/{id}/tracks an image per track
# - GET /genres/{slug}/categories lists the seeded category
# - GET /genre-playlists/{playlist_id}/tracks lists the seeded tracks in
#   position order
#
# - A catalog track with an unlinked secondary artist is read with id: null
#   by GET /genre-playlists/{id}/tracks
#
# - genre_playlist_track_ids (used by GET /tracks/{id}/upnext) reads the
#   playlist's provider ids with the user client
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_genres_integration.py -v
#
# SEE: routes/genres.py, services/genre_service.py

from uuid import uuid4

import pytest

from core.database import get_user_client
from services.genre_service import genre_playlist_track_ids

pytestmark = pytest.mark.integration

_IMAGE = {
    "small": "https://lh3.googleusercontent.com/abc=w192-h192-p-l90-rj",
    "medium": "https://lh3.googleusercontent.com/abc=w600-h600-p-l90-rj",
    "large": "https://lh3.googleusercontent.com/abc=w1320-h1320-p-l90-rj",
    "color": None,
}


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


def test_get_genre_playlists_reads_the_mosaic_of_each_of_two_playlists(
    client, make_user, admin, seed_genre_playlist, track_payload
):
    user = make_user()
    seeded = seed_genre_playlist(track_count=1)
    other = (
        admin.table("genre_playlists")
        .insert(
            {
                "genre_id": seeded["genre"]["id"],
                "title": "Second playlist",
                "category": "Second category",
            }
        )
        .execute()
        .data[0]
    )
    cover = f"https://lh3.googleusercontent.com/second-{uuid4().hex}"
    track = track_payload(thumbnail_url=f"{cover}=w60-h60-l90-rj")
    admin.table("tracks").upsert([track], on_conflict="track_id").execute()
    admin.table("genre_playlist_tracks").insert(
        {"playlist_id": other["id"], "track_id": track["track_id"], "position": 1}
    ).execute()

    response = client.get(
        f"/genres/{seeded['genre']['slug']}/playlists", headers=user.headers
    )

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["data"]["items"]}
    assert set(items) == {seeded["playlist"]["id"], other["id"]}
    assert items[other["id"]]["thumbnail_urls"] == [f"{cover}=w544-h544-p-l90-rj"]
    assert items[other["id"]]["mosaic"] == [
        {
            "small": f"{cover}=w192-h192-p-l90-rj",
            "medium": f"{cover}=w600-h600-p-l90-rj",
            "large": f"{cover}=w1320-h1320-p-l90-rj",
            "color": None,
        }
    ]
    assert items[seeded["playlist"]["id"]]["mosaic"] == [_IMAGE]
    assert items[seeded["playlist"]["id"]]["thumbnail_urls"] == [
        "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"
    ]


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
    assert [item["image"] for item in items] == [_IMAGE] * 3


def test_get_genre_playlist_tracks_reads_an_unlinked_secondary(
    client, make_user, seed_genre_playlist, admin
):
    user = make_user()
    seeded = seed_genre_playlist(track_count=1)
    [track] = seeded["tracks"]
    artists = [
        {"id": "artist-1", "name": "Some Artist"},
        {"id": None, "name": "Second Artist"},
    ]
    admin.table("tracks").update({"artists": artists}).eq(
        "track_id", track["track_id"]
    ).execute()

    response = client.get(
        f"/genre-playlists/{seeded['playlist']['id']}/tracks", headers=user.headers
    )

    assert response.status_code == 200
    [item] = response.json()["data"]["items"]
    assert item["artists"] == artists


def test_genre_playlist_track_ids_reads_with_the_user_client(
    make_user, seed_genre_playlist
):
    user = make_user()
    seeded = seed_genre_playlist(track_count=3)

    result = genre_playlist_track_ids(
        get_user_client(user.token), seeded["playlist"]["id"]
    )

    assert result == frozenset(t["track_id"] for t in seeded["tracks"])
