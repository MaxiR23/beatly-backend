# test/integration/test_onboarding_integration.py
#
# Integration tests of the onboarding endpoints, of the seed script's write
# and of migrations 049 and 050 against the local stack.
#
# Tested:
# - GET /onboarding/artists returns the rows ordered by position, paginated
#   with the cursor (total only on the first page), with the image tiers
# - GET with an empty table is an empty first page
# - replace_onboarding_artists replaces the whole set, and a failing INSERT
#   (repeated id) keeps the previous set
# - The write of the seed script stores its rows through the real function
# - anon and authenticated can neither touch the table nor call the function
#   (42501)
# - POST /onboarding/complete with five curated ids stores five library rows
#   (kind artist, source external) and the timestamp, which GET /profile/me
#   returns
# - A second POST keeps one row per artist and the first timestamp
# - An artist already in the library stays one row
# - An artist outside the table that the provider confirms gone is skipped
#   and the rest is stored; four valid plus one gone is 422 too_few_artists
#   and nothing is written
# - An id with characters that break the in_ filter is 422 invalid_request
# - authenticated cannot update onboarding_completed_at directly (42501) but
#   still updates its display_name (the column-level grant of 050)
#
# What is covered:
# - The queries, the function and the privileges of migrations 049 and 050
#   against the real schema. The provider is replaced at the module level of
#   the service (get_artist and the probe): nothing goes out to it
#
# Run with: pytest -m integration test/integration/test_onboarding_integration.py -v
#
# SEE: routes/onboarding.py, services/onboarding_service.py,
#      scripts/seed_onboarding_artists.py,
#      db/migrations/049_onboarding_artists.sql,
#      db/migrations/050_profiles_onboarding_completed_at.sql

import pytest
from postgrest.exceptions import APIError
from supabase import create_client

from core.config import settings
from core.database import get_user_client
from core.exceptions import UpstreamError
from core.search_provider import ProviderResourceMissing
from models.artist import Artist
from scripts import seed_onboarding_artists
from services import onboarding_service

pytestmark = pytest.mark.integration

_THUMB = "https://lh3.googleusercontent.com/it=w60-h60-l90-rj"


def _curated(count=5, start=1):
    return [
        {
            "id": f"UCcur{i}",
            "name": f"Curated {i}",
            "thumbnail_url": _THUMB,
            "position": i,
        }
        for i in range(start, start + count)
    ]


def _set_table(admin, rows):
    # The table is global: every test fixes its own state first.
    assert admin.rpc(
        "replace_onboarding_artists", {"p_rows": rows}
    ).execute().data == len(rows)


def _ids(rows):
    return [r["id"] for r in rows]


def _install(monkeypatch, *, gone=()):
    def get_artist(provider, cache, artist_id):
        if artist_id in gone:
            error = UpstreamError()
            error.__cause__ = KeyError("contents")
            raise error
        return Artist(id=artist_id, name=f"Name {artist_id}", thumbnail_url=_THUMB), 1

    def probe(provider, browse_id, cause):
        if browse_id in gone:
            raise ProviderResourceMissing(browse_id) from cause
        raise AssertionError(f"probed an id that is not gone: {browse_id}")

    monkeypatch.setattr(onboarding_service, "get_artist", get_artist)
    monkeypatch.setattr(onboarding_service, "provider_raise_if_browse_missing", probe)


def _library(admin, user):
    return (
        admin.table("library_items")
        .select("*")
        .eq("user_id", user.user_id)
        .order("external_id")
        .execute()
        .data
    )


def _completed_at(admin, user):
    return (
        admin.table("profiles")
        .select("onboarding_completed_at")
        .eq("id", user.user_id)
        .execute()
        .data[0]["onboarding_completed_at"]
    )


def _complete(client, user, ids):
    return client.post(
        "/onboarding/complete", json={"artist_ids": ids}, headers=user.headers
    )


# --- GET /onboarding/artists -------------------------------------------------


def test_get_onboarding_artists_returns_the_rows_ordered_by_position(
    client, make_user, admin
):
    user = make_user()
    # Inserted out of order on purpose: the order is the position.
    _set_table(admin, list(reversed(_curated(5))))

    first = client.get("/onboarding/artists?limit=2", headers=user.headers)

    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert _ids(data["items"]) == ["UCcur1", "UCcur2"]
    assert data["page"]["has_more"] is True
    assert data["page"]["total"] == 5
    item = data["items"][0]
    assert item["name"] == "Curated 1"
    assert item["image"]["small"].startswith("https://lh3.googleusercontent.com/it=")
    assert item["image"]["color"] is None
    assert "position" not in item

    second = client.get(
        f"/onboarding/artists?limit=2&cursor={data['page']['next_cursor']}",
        headers=user.headers,
    )

    page = second.json()["data"]
    assert _ids(page["items"]) == ["UCcur3", "UCcur4"]
    assert page["page"]["total"] is None
    assert page["page"]["has_more"] is True


def test_get_onboarding_artists_of_an_empty_table_is_an_empty_first_page(
    client, make_user, admin
):
    user = make_user()
    _set_table(admin, [])

    response = client.get("/onboarding/artists", headers=user.headers)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "items": [],
        "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 0},
    }


# --- replace_onboarding_artists and the seed script --------------------------


def test_replace_onboarding_artists_replaces_the_whole_set(admin):
    _set_table(admin, _curated(3))

    _set_table(admin, _curated(2, start=10))

    stored = admin.table("onboarding_artists").select("*").order("position").execute()
    assert [(r["id"], r["position"]) for r in stored.data] == [
        ("UCcur10", 10),
        ("UCcur11", 11),
    ]


def test_replace_onboarding_artists_failure_keeps_the_previous_set(admin):
    _set_table(admin, _curated(3))

    with pytest.raises(APIError) as raised:
        _set_table(
            admin,
            [
                {"id": "UCdup", "name": "A", "thumbnail_url": None, "position": 1},
                {"id": "UCdup", "name": "B", "thumbnail_url": None, "position": 2},
            ],
        )

    assert raised.value.code == "23505"
    stored = admin.table("onboarding_artists").select("id").order("position").execute()
    assert _ids(stored.data) == ["UCcur1", "UCcur2", "UCcur3"]


def test_replace_onboarding_artists_rejects_a_malformed_id_and_keeps_the_set(admin):
    _set_table(admin, _curated(2))

    with pytest.raises(APIError) as raised:
        _set_table(
            admin,
            [{"id": "nope", "name": "A", "thumbnail_url": None, "position": 1}],
        )

    assert raised.value.code == "23514"
    stored = admin.table("onboarding_artists").select("id").execute()
    assert len(stored.data) == 2


def test_the_seed_script_write_stores_the_rows(admin):
    _set_table(admin, [])
    rows = [
        {"id": "UCone", "name": "One", "thumbnail_url": _THUMB, "position": 1},
        {"id": "UCtwo", "name": "Two", "thumbnail_url": None, "position": 2},
    ]

    assert seed_onboarding_artists.write(admin, rows) == 2

    stored = admin.table("onboarding_artists").select("*").order("position").execute()
    assert stored.data == rows


def test_anon_and_authenticated_cannot_touch_the_table_or_call_the_function(
    make_user,
):
    clients = [
        create_client(settings.supabase_url, settings.supabase_anon_key),
        get_user_client(make_user().token),
    ]

    for client in clients:
        with pytest.raises(APIError) as selected:
            client.table("onboarding_artists").select("*").execute()
        with pytest.raises(APIError) as inserted:
            client.table("onboarding_artists").insert(_curated(1)[0]).execute()
        with pytest.raises(APIError) as called:
            client.rpc("replace_onboarding_artists", {"p_rows": []}).execute()
        assert selected.value.code == "42501"
        assert inserted.value.code == "42501"
        assert called.value.code == "42501"


# --- POST /onboarding/complete -----------------------------------------------


def test_complete_with_five_curated_ids_stores_five_library_rows_and_the_timestamp(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _set_table(admin, _curated(5))
    _install(monkeypatch)
    assert _completed_at(admin, user) is None

    response = _complete(client, user, [f"UCcur{i}" for i in range(1, 6)])

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["saved"] == 5
    assert data["skipped"] == []
    rows = _library(admin, user)
    assert [r["external_id"] for r in rows] == [f"UCcur{i}" for i in range(1, 6)]
    assert {(r["kind"], r["source"]) for r in rows} == {("artist", "external")}
    assert rows[0]["title"] == "Curated 1"
    assert rows[0]["thumbnail_url"] == _THUMB
    stored = _completed_at(admin, user)
    assert stored is not None
    profile = client.get("/profile/me", headers=user.headers).json()["data"]
    assert profile["onboarding_completed_at"] == data["onboarding_completed_at"]


def test_complete_twice_keeps_one_row_per_artist_and_the_first_timestamp(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _set_table(admin, _curated(5))
    _install(monkeypatch)
    ids = [f"UCcur{i}" for i in range(1, 6)]

    first = _complete(client, user, ids).json()["data"]
    second = _complete(client, user, ids)

    assert second.status_code == 200, second.text
    assert (
        second.json()["data"]["onboarding_completed_at"]
        == (first["onboarding_completed_at"])
    )
    assert len(_library(admin, user)) == 5


def test_complete_keeps_one_row_for_an_artist_already_in_the_library(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _set_table(admin, _curated(5))
    _install(monkeypatch)
    added = client.post(
        "/library",
        json={
            "kind": "artist",
            "external_id": "UCcur1",
            "title": "Old title",
            "source": "external",
        },
        headers=user.headers,
    )
    assert added.status_code == 200, added.text

    response = _complete(client, user, [f"UCcur{i}" for i in range(1, 6)])

    assert response.status_code == 200, response.text
    rows = _library(admin, user)
    assert len(rows) == 5
    assert rows[0]["external_id"] == "UCcur1"
    assert rows[0]["title"] == "Curated 1"


def test_complete_with_a_mixed_batch_of_photo_and_no_photo_stores_every_row(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    rows = _curated(5)
    rows[0]["thumbnail_url"] = None
    rows[1]["thumbnail_url"] = None
    _set_table(admin, rows)
    _install(monkeypatch)

    response = _complete(client, user, _ids(rows))

    assert response.status_code == 200, response.text
    assert response.json()["data"]["saved"] == 5
    thumbs = {r["external_id"]: r["thumbnail_url"] for r in _library(admin, user)}
    assert thumbs == {
        "UCcur1": "",
        "UCcur2": "",
        "UCcur3": _THUMB,
        "UCcur4": _THUMB,
        "UCcur5": _THUMB,
    }


def test_complete_keeps_the_stored_photo_of_an_artist_that_comes_without_one(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    rows = _curated(5)
    rows[0]["thumbnail_url"] = None
    _set_table(admin, rows)
    _install(monkeypatch)
    stored = "https://lh3.googleusercontent.com/stored=w60-h60-l90-rj"
    admin.table("library_items").insert(
        {
            "user_id": user.user_id,
            "kind": "artist",
            "external_id": "UCcur1",
            "title": "Old title",
            "source": "external",
            "thumbnail_url": stored,
        }
    ).execute()

    response = _complete(client, user, _ids(rows))

    assert response.status_code == 200, response.text
    assert response.json()["data"]["saved"] == 5
    by_id = {r["external_id"]: r for r in _library(admin, user)}
    assert len(by_id) == 5
    assert by_id["UCcur1"]["thumbnail_url"] == stored
    assert by_id["UCcur1"]["title"] == "Curated 1"
    assert by_id["UCcur2"]["thumbnail_url"] == _THUMB


def test_complete_with_ids_outside_the_table_resolves_them_from_the_provider(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _set_table(admin, _curated(3))
    _install(monkeypatch)

    response = _complete(
        client, user, ["UCcur1", "UCcur2", "UCcur3", "UCfound1", "UCfound2"]
    )

    assert response.status_code == 200, response.text
    titles = {r["external_id"]: r["title"] for r in _library(admin, user)}
    assert titles["UCfound1"] == "Name UCfound1"
    assert titles["UCcur1"] == "Curated 1"


def test_complete_skips_a_missing_artist_and_stores_the_rest(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _set_table(admin, _curated(5))
    _install(monkeypatch, gone={"UCgone"})

    response = _complete(client, user, [*(f"UCcur{i}" for i in range(1, 6)), "UCgone"])

    assert response.status_code == 200, response.text
    assert response.json()["data"]["skipped"] == ["UCgone"]
    assert response.json()["data"]["saved"] == 5
    assert "UCgone" not in {r["external_id"] for r in _library(admin, user)}


def test_complete_with_four_valid_and_one_missing_writes_nothing(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _set_table(admin, _curated(5))
    _install(monkeypatch, gone={"UCgone"})

    response = _complete(client, user, [*(f"UCcur{i}" for i in range(1, 5)), "UCgone"])

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "too_few_artists"}
    assert _library(admin, user) == []
    assert _completed_at(admin, user) is None


def test_complete_with_an_id_that_breaks_the_in_filter_is_invalid_request(
    client, make_user, admin, monkeypatch
):
    user = make_user()
    _set_table(admin, _curated(5))
    _install(monkeypatch)

    response = _complete(client, user, [*(f"UCcur{i}" for i in range(1, 5)), 'UCa",b)'])

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert _library(admin, user) == []


# --- migration 050 -----------------------------------------------------------


def test_authenticated_cannot_set_onboarding_completed_at(make_user):
    user = make_user()
    db = get_user_client(user.token)

    with pytest.raises(APIError) as raised:
        db.table("profiles").update(
            {"onboarding_completed_at": "2026-01-01T00:00:00+00:00"}
        ).eq("id", user.user_id).execute()

    assert raised.value.code == "42501"


def test_authenticated_can_still_update_its_display_name_directly(make_user, admin):
    user = make_user()
    db = get_user_client(user.token)

    response = (
        db.table("profiles")
        .update({"display_name": "Direct"})
        .eq("id", user.user_id)
        .execute()
    )

    assert response.data[0]["display_name"] == "Direct"
