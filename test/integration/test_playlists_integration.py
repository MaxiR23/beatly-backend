# test/integration/test_playlists_integration.py
#
# Integration tests of the playlists endpoints against the local stack.
#
# Tested:
# - POST /playlists creates a playlist owned by the caller
# - GET /playlists lists the caller's playlists
# - image / mosaic carry the 192 / 600 / 1320 tiers next to thumbnail_url /
#   thumbnail_urls on GET /playlists, GET /playlists/{id}, the two track
#   reads and POST /playlists/{id}/tracks
# - GET /playlists with two playlists reads each one's own mosaic through
#   the get_user_playlist_thumbnails RPC over GET with a two-id array
#   literal
# - GET /playlists/{id}, /tracks and /track-ids read a playlist with tracks;
#   the detail carries the mosaic and the duration total, both RPCs over GET
# - GET /playlists/owned-with-track/{track_id} lists the playlists that
#   hold the track
# - GET /playlists/liked, /liked/tracks and /liked/track-ids read the
#   virtual liked playlist
# - PATCH and DELETE /playlists/{id} update and remove a playlist
# - POST /playlists/{id}/tracks and /tracks/bulk add tracks, writing the
#   catalog
# - DELETE /playlists/{id}/tracks/{track_id} removes a track
# - POST /playlists/{id}/move-track reorders the tracks
# - PATCH /playlists/{id} with a new title renames the playlist's recents
#   of every user, keeping played_at and order; GET /recents adds
#   metadata.image and the stored row stays without it
# - PATCH without a title change leaves recents untouched
# - DELETE /playlists/{id} removes the playlist's recents of every user
# - Other recents (other playlists, album, genre, liked) are not touched
#
# - The track artists rule: a track with an unlinked secondary artist round
#   trips through POST /playlists/{id}/tracks and the two /tracks reads with
#   id: null; a bulk with one unlinked first artist is 422 and writes
#   nothing
#
# - owned_playlist_track_ids and liked_track_ids (used by GET
#   /tracks/{id}/upnext) read the provider ids with the user client, ignore
#   another user's public playlist and an unknown one, and skip unliked
#   tracks
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
# - The 039 triggers on playlists, running under the caller's JWT
#
# Run with: pytest -m integration test/integration/test_playlists_integration.py -v
#
# SEE: routes/playlists.py, services/playlist_service.py,
# db/migrations/039_sync_recents_with_own_playlists.sql

from uuid import uuid4

import pytest

from core.database import get_user_client
from services.playlist_service import liked_track_ids, owned_playlist_track_ids

pytestmark = pytest.mark.integration

# What the track_payload thumbnail (a 60 px URL) becomes: the 544 rewrite and
# its tiers.
_THUMBNAIL_544 = "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"
_IMAGE = {
    "small": "https://lh3.googleusercontent.com/abc=w192-h192-p-l90-rj",
    "medium": "https://lh3.googleusercontent.com/abc=w600-h600-p-l90-rj",
    "large": "https://lh3.googleusercontent.com/abc=w1320-h1320-p-l90-rj",
    "color": None,
}


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


def test_get_playlists_reads_the_mosaic_of_each_of_two_playlists(
    client, make_user, track_payload
):
    user = make_user()
    covers = {}
    for name in ("first", "second"):
        playlist = _create(client, user, title=name)
        cover = f"https://lh3.googleusercontent.com/{name}-{uuid4().hex}"
        _add(
            client,
            user,
            playlist["id"],
            track_payload(thumbnail_url=f"{cover}=w60-h60-l90-rj"),
        )
        covers[playlist["id"]] = f"{cover}=w544-h544-p-l90-rj"

    response = client.get("/playlists", headers=user.headers)

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert {item["id"] for item in items} == set(covers)
    for item in items:
        assert item["thumbnail_urls"] == [covers[item["id"]]]
        [tiers] = item["mosaic"]
        assert tiers["large"] == covers[item["id"]].replace(
            "=w544-h544", "=w1320-h1320"
        )


def test_get_playlist_returns_the_detail_with_totals(client, make_user, track_payload):
    user = make_user()
    playlist, tracks = _playlist_with_tracks(client, user, track_payload, count=2)

    response = client.get(f"/playlists/{playlist['id']}", headers=user.headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == playlist["id"]
    assert data["total_count"] == 2
    assert data["total_duration_seconds"] == sum(t["duration_seconds"] for t in tracks)
    # The one-id array literal of the mosaic RPC, over GET.
    assert len(data["thumbnail_urls"]) >= 1
    assert len(data["mosaic"]) == len(data["thumbnail_urls"])


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
    assert {item["image"]["small"] for item in items} == {_IMAGE["small"]}


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
    assert all(item["image"] == _IMAGE for item in items)


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
    assert data["thumbnail_url"] == _THUMBNAIL_544
    assert data["image"] == _IMAGE
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


def _register_recent(client, user, entity_type, entity_id, title, kind=None):
    metadata = {
        "title": title,
        "subtitle": "Sub",
        "thumbnail_url": "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj",
    }
    if kind is not None:
        metadata["kind"] = kind
    response = client.post(
        "/recents",
        json={
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata": metadata,
        },
        headers=user.headers,
    )
    assert response.status_code == 200


def _recent_row(admin, user_id, entity_type, entity_id):
    return (
        admin.table("recent_activity")
        .select("metadata, played_at")
        .eq("user_id", user_id)
        .eq("entity_type", entity_type)
        .eq("entity_id", entity_id)
        .execute()
        .data
    )


def _recents(client, user):
    response = client.get("/recents", headers=user.headers)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    return response.json()["data"]


def test_patch_title_renames_the_playlist_recent_keeping_played_at_and_order(
    client, make_user, admin
):
    user = make_user()
    playlist = _create(client, user)
    older, newer = f"it-{uuid4().hex}", f"it-{uuid4().hex}"
    _register_recent(client, user, "album", older, "Older")
    _register_recent(client, user, "playlist", playlist["id"], "Mix", kind="user")
    _register_recent(client, user, "album", newer, "Newer")
    [before] = _recent_row(admin, user.user_id, "playlist", playlist["id"])

    response = client.patch(
        f"/playlists/{playlist['id']}", json={"title": "Renamed"}, headers=user.headers
    )

    assert response.status_code == 200
    items = _recents(client, user)["items"]
    assert [i["entity_id"] for i in items] == [newer, playlist["id"], older]
    item = items[1]
    # image is derived on the way out; the stored row below has none.
    assert item["metadata"] == {
        **before["metadata"],
        "title": "Renamed",
        "image": {
            "small": "https://lh3.googleusercontent.com/abc=w192-h192-p-l90-rj",
            "medium": "https://lh3.googleusercontent.com/abc=w600-h600-p-l90-rj",
            "large": "https://lh3.googleusercontent.com/abc=w1320-h1320-p-l90-rj",
            "color": None,
        },
    }
    assert item["metadata"]["kind"] == "user"
    assert item["played_at"] == before["played_at"]
    [after] = _recent_row(admin, user.user_id, "playlist", playlist["id"])
    assert after == {
        "metadata": {**before["metadata"], "title": "Renamed"},
        "played_at": before["played_at"],
    }


def test_patch_title_renames_another_users_recent_of_the_playlist(
    client, make_user, admin
):
    owner, other = make_user(), make_user()
    playlist = _create(client, owner, is_public=True)
    _register_recent(client, other, "playlist", playlist["id"], "Mix", kind="user")
    [before] = _recent_row(admin, other.user_id, "playlist", playlist["id"])

    response = client.patch(
        f"/playlists/{playlist['id']}", json={"title": "Renamed"}, headers=owner.headers
    )

    assert response.status_code == 200
    [item] = _recents(client, other)["items"]
    assert item["metadata"]["title"] == "Renamed"
    [after] = _recent_row(admin, other.user_id, "playlist", playlist["id"])
    assert after["played_at"] == before["played_at"]


def test_patch_without_a_title_change_leaves_the_recents_untouched(
    client, make_user, admin
):
    owner, other = make_user(), make_user()
    playlist = _create(client, owner, is_public=True)
    for user in (owner, other):
        _register_recent(client, user, "playlist", playlist["id"], "Mix", kind="user")

    def snapshot():
        return [
            _recent_row(admin, user.user_id, "playlist", playlist["id"])
            for user in (owner, other)
        ]

    before = snapshot()
    for body in ({"description": "New text"}, {"is_public": False}, {"title": "Mix"}):
        response = client.patch(
            f"/playlists/{playlist['id']}", json=body, headers=owner.headers
        )
        assert response.status_code == 200
        assert snapshot() == before


def test_delete_playlist_removes_its_only_recent_leaving_an_empty_page(
    client, make_user
):
    user = make_user()
    playlist = _create(client, user)
    _register_recent(client, user, "playlist", playlist["id"], "Mix", kind="user")
    assert _recents(client, user)["page"]["total"] == 1

    response = client.delete(f"/playlists/{playlist['id']}", headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    assert _recents(client, user) == {
        "items": [],
        "page": {"limit": 30, "next_cursor": None, "has_more": False, "total": 0},
    }


def test_delete_playlist_removes_its_recent_and_keeps_the_others(
    client, make_user, admin
):
    user, other = make_user(), make_user()
    doomed = _create(client, user, is_public=True)
    kept = _create(client, user)
    album, genre = f"it-{uuid4().hex}", f"it-{uuid4().hex}"
    _register_recent(client, user, "playlist", doomed["id"], "Doomed", kind="user")
    _register_recent(client, user, "playlist", kept["id"], "Kept", kind="user")
    _register_recent(client, user, "album", album, "Album")
    _register_recent(client, user, "playlist", genre, "Genre", kind="genre")
    _register_recent(client, user, "playlist", "liked", "Liked", kind="liked")
    _register_recent(client, other, "playlist", doomed["id"], "Doomed", kind="user")
    _register_recent(client, other, "album", album, "Album")
    keep = [
        (user, "playlist", kept["id"]),
        (user, "album", album),
        (user, "playlist", genre),
        (user, "playlist", "liked"),
        (other, "album", album),
    ]

    def snapshot():
        return [_recent_row(admin, u.user_id, t, e) for u, t, e in keep]

    before = snapshot()
    total_before = _recents(client, user)["page"]["total"]
    renamed = client.patch(
        f"/playlists/{doomed['id']}", json={"title": "Gone"}, headers=user.headers
    )
    assert renamed.status_code == 200

    response = client.delete(f"/playlists/{doomed['id']}", headers=user.headers)

    assert response.status_code == 200
    data = _recents(client, user)
    assert data["page"]["total"] == total_before - 1
    assert doomed["id"] not in [i["entity_id"] for i in data["items"]]
    for u in (user, other):
        assert _recent_row(admin, u.user_id, "playlist", doomed["id"]) == []
    assert snapshot() == before


_UNLINKED_SECONDARY = [
    {"id": "artist-1", "name": "Some Artist"},
    {"id": None, "name": "Second Artist"},
]


def test_post_playlist_track_with_an_unlinked_secondary_round_trips(
    client, make_user, track_payload
):
    user = make_user()
    playlist = _create(client, user)
    track = track_payload(
        artists=[{"id": "artist-1", "name": "Some Artist"}, {"name": "Second Artist"}]
    )

    added = _add(client, user, playlist["id"], track)

    assert added["artists"] == _UNLINKED_SECONDARY
    response = client.get(f"/playlists/{playlist['id']}/tracks", headers=user.headers)
    assert response.status_code == 200
    [item] = response.json()["data"]["items"]
    assert item["artists"] == _UNLINKED_SECONDARY


def test_get_liked_playlist_tracks_reads_an_unlinked_secondary(
    client, make_user, track_payload
):
    user = make_user()
    track = track_payload(
        artists=[{"id": "artist-1", "name": "Some Artist"}, {"name": "Second Artist"}]
    )
    client.post("/likes", json=track, headers=user.headers)

    response = client.get("/playlists/liked/tracks", headers=user.headers)

    assert response.status_code == 200
    [item] = response.json()["data"]["items"]
    assert item["artists"] == _UNLINKED_SECONDARY


def test_post_playlist_tracks_bulk_with_one_unlinked_primary_writes_nothing(
    client, make_user, track_payload, admin
):
    user = make_user()
    playlist = _create(client, user)
    good = track_payload()
    broken = track_payload(artists=[{"id": None, "name": "No Link"}])

    response = client.post(
        f"/playlists/{playlist['id']}/tracks/bulk",
        json={"tracks": [good, broken]},
        headers=user.headers,
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    written = (
        admin.table("tracks")
        .select("track_id")
        .in_("track_id", [good["track_id"], broken["track_id"]])
        .execute()
        .data
    )
    assert written == []
    entries = (
        admin.table("playlist_tracks")
        .select("id")
        .eq("playlist_id", playlist["id"])
        .execute()
        .data
    )
    assert entries == []


def test_owned_playlist_track_ids_returns_the_provider_ids(
    client, make_user, track_payload
):
    user = make_user()
    playlist, tracks = _playlist_with_tracks(client, user, track_payload, count=3)

    result = owned_playlist_track_ids(
        get_user_client(user.token), user.user_id, playlist["id"]
    )

    assert result == frozenset(t["track_id"] for t in tracks)


def test_owned_playlist_track_ids_ignores_another_users_public_playlist(
    client, make_user, track_payload
):
    owner = make_user()
    reader = make_user()
    playlist = _create(client, owner, is_public=True)
    _add(client, owner, playlist["id"], track_payload())

    # RLS lets the reader see it; can_edit discards it.
    result = owned_playlist_track_ids(
        get_user_client(reader.token), reader.user_id, playlist["id"]
    )

    assert result == frozenset()


def test_owned_playlist_track_ids_of_an_unknown_playlist_is_empty(make_user):
    user = make_user()

    result = owned_playlist_track_ids(
        get_user_client(user.token), user.user_id, str(uuid4())
    )

    assert result == frozenset()


def test_liked_track_ids_returns_only_active_likes(client, make_user, track_payload):
    user = make_user()
    kept, dropped = _like_two(client, user, track_payload)
    assert (
        client.delete(f"/likes/{dropped['track_id']}", headers=user.headers).status_code
        == 200
    )

    result = liked_track_ids(get_user_client(user.token), user.user_id)

    assert result == frozenset({kept["track_id"]})
