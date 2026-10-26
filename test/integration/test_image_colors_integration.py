# test/integration/test_image_colors_integration.py
#
# Integration tests of the image color table and its two functions (migration
# 042) against the local Supabase stack.
#
# Tested:
# - get_image_colors returns the stored rows and omits absent keys
# - claim_image_colors returns a new key once; two concurrent claims return
#   it to only one of them
# - A recent failure is not claimed again; a stale one is, and its
#   failed_at is cleared; a key with a color is never claimed
# - Upserting the same key twice keeps one row
# - The table refuses an uppercase color (23514) and a row with both a color
#   and a failed_at
# - anon and authenticated can neither read the table nor call either
#   function (42501)
# - A route computes the color after the response and serves it on the next
#   read, with one row and no failed_at
# - A route records a failed download and does not retry it inside the window
#
# What is covered:
# - The table, constraints, privileges and functions the migration builds,
#   through the service-role client the app uses
#
# Run with: pytest -m integration test/integration/test_image_colors_integration.py -v
#
# SEE: db/migrations/042_image_colors.sql, services/image_color_service.py

import threading
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from postgrest.exceptions import APIError
from supabase import create_client

from core.config import settings
from core.database import get_user_client
from services import image_color_service

pytestmark = pytest.mark.integration

_FAR_PAST = "2000-01-01T00:00:00+00:00"


def _key() -> str:
    return f"https://lh3.googleusercontent.com/it-{uuid4().hex}"


def _claim(admin, keys, *, attempted_at=None, retry_before=None):
    now = datetime.now(UTC)
    response = admin.rpc(
        "claim_image_colors",
        {
            "p_keys": keys,
            "p_attempted_at": (attempted_at or now).isoformat(),
            "p_retry_before": (retry_before or now - timedelta(days=1)).isoformat(),
        },
    ).execute()
    return [row["image_key"] for row in response.data]


def _row(admin, key):
    rows = admin.table("image_colors").select("*").eq("image_key", key).execute().data
    return rows


def _red_png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (192, 192), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_get_image_colors_returns_stored_colors_and_omits_absent_keys(admin):
    stored, absent = _key(), _key()
    admin.table("image_colors").insert(
        {
            "image_key": stored,
            "color": "#ee0000",
            "attempted_at": datetime.now(UTC).isoformat(),
        }
    ).execute()

    rows = admin.rpc("get_image_colors", {"p_keys": [stored, absent]}).execute().data

    assert [(r["image_key"], r["color"]) for r in rows] == [(stored, "#ee0000")]
    assert set(rows[0]) == {"image_key", "color", "attempted_at"}


def test_claim_returns_a_key_once(admin):
    key = _key()

    first = _claim(admin, [key, key])
    second = _claim(admin, [key])

    assert first == [key]
    assert second == []
    assert len(_row(admin, key)) == 1


def test_two_concurrent_claims_return_the_key_to_one_of_them(admin):
    key = _key()
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(_claim(admin, [key]))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results, key=len) == [[], [key]]


def test_claim_skips_a_recent_failure_and_takes_a_stale_one(admin):
    recent, stale, colored = _key(), _key(), _key()
    now = datetime.now(UTC).isoformat()
    admin.table("image_colors").insert(
        [
            {"image_key": recent, "attempted_at": now, "failed_at": now},
            {"image_key": stale, "attempted_at": _FAR_PAST, "failed_at": _FAR_PAST},
            {"image_key": colored, "color": "#ee0000", "attempted_at": _FAR_PAST},
        ]
    ).execute()

    claimed = _claim(admin, [recent, stale, colored])

    assert claimed == [stale]
    row = _row(admin, stale)[0]
    assert row["failed_at"] is None
    assert not row["attempted_at"].startswith("2000")
    assert _row(admin, colored)[0]["color"] == "#ee0000"


def test_upserting_the_same_key_twice_keeps_one_row(admin):
    key = _key()
    now = datetime.now(UTC).isoformat()
    row = {"image_key": key, "color": "#123456", "attempted_at": now}

    admin.table("image_colors").upsert(row, on_conflict="image_key").execute()
    admin.table("image_colors").upsert(
        {**row, "color": "#654321"}, on_conflict="image_key"
    ).execute()

    rows = _row(admin, key)
    assert len(rows) == 1
    assert rows[0]["color"] == "#654321"


def test_the_table_rejects_an_uppercase_color(admin):
    with pytest.raises(APIError) as raised:
        admin.table("image_colors").insert(
            {
                "image_key": _key(),
                "color": "#FF0000",
                "attempted_at": datetime.now(UTC).isoformat(),
            }
        ).execute()

    assert raised.value.code == "23514"


def test_the_table_rejects_a_color_together_with_failed_at(admin):
    now = datetime.now(UTC).isoformat()

    with pytest.raises(APIError) as raised:
        admin.table("image_colors").insert(
            {
                "image_key": _key(),
                "color": "#ff0000",
                "attempted_at": now,
                "failed_at": now,
            }
        ).execute()

    assert raised.value.code == "23514"


def test_anon_and_authenticated_cannot_read_the_table_or_call_the_functions(
    make_user,
):
    clients = [
        create_client(settings.supabase_url, settings.supabase_anon_key),
        get_user_client(make_user().token),
    ]
    now = datetime.now(UTC).isoformat()

    for client in clients:
        with pytest.raises(APIError) as table:
            client.table("image_colors").select("*").execute()
        with pytest.raises(APIError) as read:
            client.rpc("get_image_colors", {"p_keys": ["x"]}).execute()
        with pytest.raises(APIError) as claim:
            client.rpc(
                "claim_image_colors",
                {"p_keys": ["x"], "p_attempted_at": now, "p_retry_before": now},
            ).execute()

        assert table.value.code == "42501"
        assert read.value.code == "42501"
        assert claim.value.code == "42501"


def test_a_route_computes_and_then_serves_the_color(
    client, make_user, track_payload, admin, monkeypatch
):
    user = make_user()
    key = _key()
    track = track_payload(thumbnail_url=f"{key}=w60-h60-l90-rj")
    downloads = []

    def fetch(url):
        downloads.append(url)
        return _red_png()

    monkeypatch.setattr(image_color_service, "_fetch_image", fetch)

    posted = client.post("/likes", json=track, headers=user.headers)
    listed = client.get("/likes", headers=user.headers)

    assert posted.status_code == 200
    assert posted.json()["data"]["image"]["color"] is None
    assert listed.json()["data"]["items"][0]["image"]["color"] == "#ee0000"
    assert downloads == [f"{key}=w192-h192-p-l90-rj"]
    rows = _row(admin, key)
    assert len(rows) == 1
    assert rows[0]["failed_at"] is None


def test_a_route_records_a_failed_download_and_does_not_retry_it(
    client, make_user, track_payload, admin
):
    user = make_user()
    key = _key()
    track = track_payload(thumbnail_url=f"{key}=w60-h60-l90-rj")

    client.post("/likes", json=track, headers=user.headers)
    after_first = _row(admin, key)[0]
    listed = client.get("/likes", headers=user.headers)
    after_second = _row(admin, key)[0]

    assert after_first["color"] is None
    assert after_first["failed_at"] is not None
    assert listed.json()["data"]["items"][0]["image"]["color"] is None
    assert after_second["attempted_at"] == after_first["attempted_at"]
