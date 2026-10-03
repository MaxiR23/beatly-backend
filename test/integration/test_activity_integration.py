# test/integration/test_activity_integration.py
#
# Integration tests of the plays and recents endpoints against the local stack.
#
# Tested:
# - POST /plays records a play event for the caller
# - POST /recents registers a recent entity
# - GET /recents returns the registered entity
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_activity_integration.py -v
#
# SEE: routes/activity.py, services/activity_service.py

from uuid import uuid4

import pytest

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
