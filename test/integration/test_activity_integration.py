# test/integration/test_activity_integration.py
#
# Integration tests of the plays and recents endpoints against the local stack.
#
# Tested:
# - POST /plays records a play event for the caller
# - POST /recents registers a recent entity
# - GET /recents returns the registered entity
# - POST and GET /recents derive metadata.image (192 / 600 / 1320 tiers)
#   from the stored 512 thumbnail_url, and the stored row has no image
#
# - POST /plays stores an unlinked secondary artist with id: null
#
# - played_track_ids_since (used by GET /tracks/{id}/upnext) reads the
#   caller's recent plays with the user client, leaves out older plays and
#   other users' plays, and honours the limit
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_activity_integration.py -v
#
# SEE: routes/activity.py, services/activity_service.py

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.database import get_user_client
from services.activity_service import played_track_ids_since

pytestmark = pytest.mark.integration


def test_post_play_records_a_play_event_for_the_caller(
    client, make_user, track_payload, admin
):
    user = make_user()
    track = track_payload()

    response = client.post("/plays", json=track, headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["track_id"] == track["track_id"]
    assert body["data"]["metadata"]["title"] == track["title"]
    stored = (
        admin.table("play_events")
        .select("track_id")
        .eq("user_id", user.user_id)
        .execute()
        .data
    )
    assert stored == [{"track_id": track["track_id"]}]


def _recent_body(entity_id):
    return {
        "entity_type": "album",
        "entity_id": entity_id,
        "metadata": {"title": "Some Album", "subtitle": "Some Artist"},
    }


def test_post_recent_registers_the_entity(client, make_user):
    user = make_user()
    entity_id = f"it-{uuid4().hex}"

    response = client.post(
        "/recents", json=_recent_body(entity_id), headers=user.headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["entity_id"] == entity_id
    assert body["data"]["metadata"]["title"] == "Some Album"


def test_get_recents_returns_the_registered_entity(client, make_user):
    user = make_user()
    entity_id = f"it-{uuid4().hex}"
    client.post("/recents", json=_recent_body(entity_id), headers=user.headers)

    response = client.get("/recents", headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    [item] = body["data"]["items"]
    assert item["entity_id"] == entity_id
    assert item["entity_type"] == "album"
    assert item["metadata"]["subtitle"] == "Some Artist"


_THUMBNAIL_60 = "https://lh3.googleusercontent.com/abc=w60-h60-l90-rj"
_THUMBNAIL_512 = "https://lh3.googleusercontent.com/abc=w512-h512-l90-rj"
_TIERS = {
    "small": "https://lh3.googleusercontent.com/abc=w192-h192-p-l90-rj",
    "medium": "https://lh3.googleusercontent.com/abc=w600-h600-p-l90-rj",
    "large": "https://lh3.googleusercontent.com/abc=w1320-h1320-p-l90-rj",
    "color": None,
}


def test_post_and_get_recents_derive_image_without_storing_it(client, make_user, admin):
    user = make_user()
    entity_id = f"it-{uuid4().hex}"
    body = _recent_body(entity_id)
    body["metadata"] = {**body["metadata"], "thumbnail_url": _THUMBNAIL_60}

    posted = client.post("/recents", json=body, headers=user.headers)

    assert posted.status_code == 200
    metadata = posted.json()["data"]["metadata"]
    assert metadata["thumbnail_url"] == _THUMBNAIL_512
    assert metadata["image"] == _TIERS
    listed = client.get("/recents", headers=user.headers)
    assert listed.status_code == 200
    [item] = listed.json()["data"]["items"]
    assert item["metadata"] == metadata
    [row] = (
        admin.table("recent_activity")
        .select("metadata")
        .eq("user_id", user.user_id)
        .execute()
        .data
    )
    assert row["metadata"] == {
        "title": "Some Album",
        "subtitle": "Some Artist",
        "thumbnail_url": _THUMBNAIL_512,
    }


def test_post_play_with_an_unlinked_secondary_stores_it(
    client, make_user, track_payload, admin
):
    user = make_user()
    track = track_payload(
        artists=[{"id": "artist-1", "name": "Some Artist"}, {"name": "Second Artist"}]
    )
    expected = [
        {"id": "artist-1", "name": "Some Artist"},
        {"id": None, "name": "Second Artist"},
    ]

    response = client.post("/plays", json=track, headers=user.headers)

    assert response.status_code == 200
    assert response.json()["data"]["metadata"]["artists"] == expected
    [stored] = (
        admin.table("play_events")
        .select("metadata")
        .eq("user_id", user.user_id)
        .execute()
        .data
    )
    assert stored["metadata"]["artists"] == expected


# Fixed dates, never the host clock: the Docker clock can drift from it.
_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
_SINCE = datetime(2001, 1, 1, tzinfo=UTC)


def _seed_play(admin, user, track_id, played_at):
    admin.table("play_events").insert(
        {
            "user_id": user.user_id,
            "track_id": track_id,
            "played_at": played_at.isoformat(),
        }
    ).execute()


def test_played_track_ids_since_returns_the_callers_recent_plays(
    client, make_user, track_payload
):
    user = make_user()
    tracks = [track_payload() for _ in range(2)]
    for track in tracks:
        assert (
            client.post("/plays", json=track, headers=user.headers).status_code == 200
        )

    result = played_track_ids_since(
        get_user_client(user.token), user.user_id, _LONG_AGO, 500
    )

    assert result == frozenset(t["track_id"] for t in tracks)


def test_played_track_ids_since_leaves_out_older_plays_and_other_users(
    client, make_user, track_payload, admin
):
    user = make_user()
    other = make_user()
    recent = track_payload()
    assert client.post("/plays", json=recent, headers=user.headers).status_code == 200
    _seed_play(admin, user, f"it-{uuid4().hex}", _LONG_AGO)
    others = track_payload()
    assert client.post("/plays", json=others, headers=other.headers).status_code == 200

    result = played_track_ids_since(
        get_user_client(user.token), user.user_id, _SINCE, 500
    )

    assert result == frozenset({recent["track_id"]})


def test_played_track_ids_since_honours_the_limit(make_user, admin):
    user = make_user()
    ids = [f"it-{uuid4().hex}" for _ in range(3)]
    for day, track_id in enumerate(ids, start=1):
        _seed_play(admin, user, track_id, datetime(2010, 1, day, tzinfo=UTC))

    result = played_track_ids_since(
        get_user_client(user.token), user.user_id, _SINCE, 1
    )

    # The most recent one.
    assert result == frozenset({ids[2]})
