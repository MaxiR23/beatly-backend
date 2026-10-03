# test/integration/test_profile_integration.py
#
# Integration tests of the profile endpoints against the local stack.
#
# Tested:
# - GET /profile/me returns the profile the signup trigger created
# - PATCH /profile/me stores username and display_name
# - A display_name outside 1..50 characters is 422 invalid_request and
#   leaves the row alone; the limits (multibyte included) are stored, so
#   the model is not stricter than profiles_display_name_length
# - A null display_name clears it
#
# What is covered:
# - The queries and RPCs of these routes against the real schema, with RLS
#   running as the caller
#
# Run with: pytest -m integration test/integration/test_profile_integration.py -v
#
# SEE: routes/profile.py, services/profile_service.py, models/profiles.py

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _display_name(admin, user):
    return (
        admin.table("profiles")
        .select("display_name")
        .eq("id", user.user_id)
        .execute()
        .data[0]["display_name"]
    )


def test_get_my_profile_returns_the_profile_of_the_caller(client, make_user):
    user = make_user()

    response = client.get("/profile/me", headers=user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["id"] == user.user_id
    assert body["data"]["role"] == "user"


def test_patch_my_profile_stores_username_and_display_name(client, make_user, admin):
    user = make_user()
    username = f"it_{uuid4().hex[:12]}"

    response = client.patch(
        "/profile/me",
        json={"username": username, "display_name": "Integration User"},
        headers=user.headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["username"] == username
    assert _display_name(admin, user) == "Integration User"


@pytest.mark.parametrize("display_name", ["", "a" * 51, "é" * 51])
def test_patch_profile_display_name_out_of_range_is_invalid_request(
    client, make_user, admin, display_name
):
    user = make_user()
    client.patch("/profile/me", json={"display_name": "Before"}, headers=user.headers)

    response = client.patch(
        "/profile/me", json={"display_name": display_name}, headers=user.headers
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert _display_name(admin, user) == "Before"


@pytest.mark.parametrize("display_name", ["a" * 50, "é" * 50, "\U0001f600" * 50])
def test_patch_profile_display_name_at_the_limits_is_stored(
    client, make_user, admin, display_name
):
    user = make_user()

    response = client.patch(
        "/profile/me", json={"display_name": display_name}, headers=user.headers
    )

    assert response.status_code == 200
    assert _display_name(admin, user) == display_name


def test_patch_profile_null_display_name_clears_it(client, make_user, admin):
    user = make_user()
    client.patch("/profile/me", json={"display_name": "Before"}, headers=user.headers)

    response = client.patch(
        "/profile/me", json={"display_name": None}, headers=user.headers
    )

    assert response.status_code == 200
    assert _display_name(admin, user) is None
