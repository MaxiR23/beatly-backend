# test/integration/test_image_colors_integration.py
#
# Integration tests of the image color table and its two functions (migrations
# 042, 043 and 044) against the local Supabase stack.
#
# Tested:
# - get_image_colors returns the stored rows and omits absent keys, and
#   returns failed_at (null for a key in progress, set for a failure)
# - A key in progress (no color, no failed_at, claimed now) is not claimed
#   again by a route, and the read leaves awaiting_colors true; a recent
#   failure does not
# - claim_image_colors returns a new key once; two concurrent claims return
#   it to only one of them
# - A recent failure is not claimed again; a stale one is, and its
#   failed_at is cleared; a key with a color is never claimed
# - With p_abandoned_before, claim_image_colors takes back only the key with
#   no color and no failed_at claimed before it; two concurrent claims of
#   such a key return it to one of them; the call of three arguments never
#   takes it back
# - A route claims again and computes a key abandoned for over 30 seconds
# - A failure written after another job stored the color leaves the color
#   and a null failed_at; one written after another job claimed the key
#   again leaves that claim and a null failed_at
# - Upserting the same key twice keeps one row
# - The table refuses an uppercase color (23514) and a row with both a color
#   and a failed_at
# - anon and authenticated can neither read the table nor call either
#   function (42501)
# - A route computes the color after the response and serves it on the next
#   read, with one row and no failed_at
# - A route records a failed download and does not retry it inside the window
# - A route writes each image with its own row, and one failed download does
#   not block the others of the batch
# - A route claims and computes more images than the pool has threads
#
# What is covered:
# - The table, constraints, privileges and functions the migration builds,
#   through the service-role client the app uses
#
# Run with: pytest -m integration test/integration/test_image_colors_integration.py -v
#
# SEE: db/migrations/042_image_colors.sql,
# db/migrations/043_get_image_colors_failed_at.sql,
# db/migrations/044_claim_image_colors_abandoned.sql,
# services/image_color_service.py

import contextvars
import threading
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from postgrest.exceptions import APIError
from pydantic import BaseModel, computed_field
from supabase import create_client

from core.config import settings
from core.database import get_user_client
from core.image_color_scope import open_image_color_scope
from core.thumbnails import image_tiers
from models.images import ImageTiers
from services import image_color_service
from services.image_color_service import load_image_colors

pytestmark = pytest.mark.integration

_FAR_PAST = "2000-01-01T00:00:00+00:00"


def _key() -> str:
    return f"https://lh3.googleusercontent.com/it-{uuid4().hex}"


def _claim(admin, keys, *, attempted_at=None, retry_before=None, abandoned_before=None):
    now = datetime.now(UTC)
    params = {
        "p_keys": keys,
        "p_attempted_at": (attempted_at or now).isoformat(),
        "p_retry_before": (retry_before or now - timedelta(days=1)).isoformat(),
    }
    # Only when given: the other callers keep the call of three arguments,
    # which is what the code deployed before 044 sends.
    if abandoned_before is not None:
        params["p_abandoned_before"] = abandoned_before.isoformat()
    response = admin.rpc("claim_image_colors", params).execute()
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
    assert set(rows[0]) == {"image_key", "color", "attempted_at", "failed_at"}


def test_get_image_colors_returns_failed_at(admin):
    failed, in_progress = _key(), _key()
    now = datetime.now(UTC).isoformat()
    admin.table("image_colors").insert(
        [
            {"image_key": failed, "attempted_at": now, "failed_at": now},
            {"image_key": in_progress, "attempted_at": now},
        ]
    ).execute()

    rows = admin.rpc("get_image_colors", {"p_keys": [failed, in_progress]}).execute()
    by_key = {r["image_key"]: r for r in rows.data}

    assert by_key[failed]["color"] is None
    assert by_key[failed]["failed_at"] is not None
    assert by_key[in_progress]["color"] is None
    assert by_key[in_progress]["failed_at"] is None


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


def _ago(seconds):
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def test_claim_takes_back_an_abandoned_key_only(admin):
    abandoned, in_progress, failed, colored = _key(), _key(), _key(), _key()
    admin.table("image_colors").insert(
        [
            {"image_key": abandoned, "attempted_at": _ago(60)},
            {"image_key": in_progress, "attempted_at": _ago(5)},
            {"image_key": failed, "attempted_at": _ago(60), "failed_at": _ago(60)},
            {"image_key": colored, "color": "#ee0000", "attempted_at": _FAR_PAST},
        ]
    ).execute()
    keys = [abandoned, in_progress, failed, colored]
    before = {key: _row(admin, key)[0] for key in keys}

    claimed = _claim(
        admin,
        keys,
        abandoned_before=datetime.now(UTC) - timedelta(seconds=30),
    )

    assert claimed == [abandoned]
    after = {key: _row(admin, key)[0] for key in keys}
    assert after[abandoned]["attempted_at"] != before[abandoned]["attempted_at"]
    assert after[abandoned]["failed_at"] is None
    for key in (in_progress, failed, colored):
        assert after[key] == before[key]


def test_two_concurrent_claims_of_an_abandoned_key_return_it_to_one_of_them(admin):
    key = _key()
    admin.table("image_colors").insert(
        {"image_key": key, "attempted_at": _ago(60)}
    ).execute()
    results = []
    barrier = threading.Barrier(2)
    abandoned_before = datetime.now(UTC) - timedelta(seconds=30)

    def worker():
        barrier.wait()
        results.append(_claim(admin, [key], abandoned_before=abandoned_before))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results, key=len) == [[], [key]]


def test_the_three_argument_claim_never_takes_an_abandoned_key(admin):
    abandoned, new = _key(), _key()
    admin.table("image_colors").insert(
        {"image_key": abandoned, "attempted_at": _ago(60)}
    ).execute()

    claimed = _claim(admin, [abandoned, new])

    assert claimed == [new]
    assert _row(admin, abandoned)[0]["attempted_at"] is not None


def test_a_late_failure_never_overwrites_a_stored_color(admin, monkeypatch):
    key = _key()
    admin.table("image_colors").insert(
        {"image_key": key, "attempted_at": _ago(60)}
    ).execute()
    now = datetime.now(UTC)
    assert _claim(admin, [key], abandoned_before=now - timedelta(seconds=30)) == [key]

    def fetch(url):
        # The first job of the key stores its color while this one downloads.
        admin.table("image_colors").upsert(
            {"image_key": key, "color": "#ee0000", "attempted_at": now.isoformat()},
            on_conflict="image_key",
        ).execute()
        raise image_color_service._ImageRejected("unexpected status")

    monkeypatch.setattr(image_color_service, "_fetch_image", fetch)

    image_color_service._compute_one(admin, key, f"{key}=w192-h192-p", now)

    rows = _row(admin, key)
    assert len(rows) == 1
    assert rows[0]["color"] == "#ee0000"
    assert rows[0]["failed_at"] is None


def test_a_late_failure_never_marks_a_claim_taken_again_as_failed(admin, monkeypatch):
    key = _key()
    first = datetime.now(UTC) - timedelta(seconds=60)
    admin.table("image_colors").insert(
        {"image_key": key, "attempted_at": first.isoformat()}
    ).execute()
    second = datetime.now(UTC)
    assert _claim(
        admin,
        [key],
        attempted_at=second,
        abandoned_before=second - timedelta(seconds=30),
    ) == [key]

    def fetch(url):
        raise image_color_service._ImageRejected("unexpected status")

    monkeypatch.setattr(image_color_service, "_fetch_image", fetch)

    image_color_service._compute_one(admin, key, f"{key}=w192-h192-p", first)

    rows = _row(admin, key)
    assert len(rows) == 1
    assert rows[0]["color"] is None
    assert rows[0]["failed_at"] is None
    assert datetime.fromisoformat(rows[0]["attempted_at"]) == second


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
        with pytest.raises(APIError) as claim_abandoned:
            client.rpc(
                "claim_image_colors",
                {
                    "p_keys": ["x"],
                    "p_attempted_at": now,
                    "p_retry_before": now,
                    "p_abandoned_before": now,
                },
            ).execute()

        assert table.value.code == "42501"
        assert read.value.code == "42501"
        assert claim.value.code == "42501"
        assert claim_abandoned.value.code == "42501"


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


def _like_without_computing(client, user, track_payload, keys):
    # With no allowed host nothing is read or computed, so the keys stay new.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "image_color_allowed_hosts", [])
        for key in keys:
            track = track_payload(thumbnail_url=f"{key}=w60-h60-l90-rj")
            assert (
                client.post("/likes", json=track, headers=user.headers).status_code
                == 200
            )


def test_a_route_writes_each_image_on_its_own_and_one_failure_does_not_block_the_others(
    client, make_user, track_payload, admin, monkeypatch
):
    user = make_user()
    keys = [_key(), _key(), _key()]
    _like_without_computing(client, user, track_payload, keys)

    def fetch(url):
        if url.startswith(keys[1]):
            raise image_color_service._ImageRejected("unexpected status")
        return _red_png()

    monkeypatch.setattr(image_color_service, "_fetch_image", fetch)

    first = client.get("/likes", headers=user.headers)
    rows = {key: _row(admin, key) for key in keys}
    second = client.get("/likes", headers=user.headers)

    assert first.status_code == 200
    assert all(len(rows[key]) == 1 for key in keys)
    assert [rows[k][0]["color"] for k in keys] == ["#ee0000", None, "#ee0000"]
    assert rows[keys[0]][0]["failed_at"] is None
    assert rows[keys[1]][0]["failed_at"] is not None
    colors = {
        item["image"]["small"].split("=")[0]: item["image"]["color"]
        for item in second.json()["data"]["items"]
    }
    assert colors == {keys[0]: "#ee0000", keys[1]: None, keys[2]: "#ee0000"}


def test_a_route_claims_and_computes_more_images_than_the_pool_size(
    client, make_user, track_payload, admin, monkeypatch
):
    monkeypatch.setattr(settings, "image_color_max_parallel_fetches", 2)
    image_color_service._pool.cache_clear()
    try:
        user = make_user()
        keys = [_key() for _ in range(6)]
        _like_without_computing(client, user, track_payload, keys)
        monkeypatch.setattr(image_color_service, "_fetch_image", lambda url: _red_png())

        client.get("/likes", headers=user.headers)

        assert [_row(admin, key)[0]["color"] for key in keys] == ["#ee0000"] * 6
    finally:
        image_color_service._pool().shutdown(wait=True)
        image_color_service._pool.cache_clear()


class _Cover(BaseModel):
    url: str

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.url)


def _read_scope(admin, *keys):
    def run():
        scope = open_image_color_scope()
        payload = [_Cover(url=f"{key}=w60-h60-l90-rj") for key in keys]
        load_image_colors(admin, scope, _Covers(items=payload))
        return scope

    return contextvars.copy_context().run(run)


class _Covers(BaseModel):
    items: list[_Cover]


def test_a_key_in_progress_is_awaited_and_not_claimed_again(
    client, make_user, track_payload, admin, monkeypatch
):
    user = make_user()
    in_progress, failed = _key(), _key()
    _like_without_computing(client, user, track_payload, [in_progress, failed])
    now = datetime.now(UTC).isoformat()
    admin.table("image_colors").insert(
        [
            {"image_key": in_progress, "attempted_at": now},
            {"image_key": failed, "attempted_at": now, "failed_at": now},
        ]
    ).execute()
    before = {key: _row(admin, key)[0] for key in (in_progress, failed)}
    downloads = []

    def fetch(url):
        downloads.append(url)
        return _red_png()

    monkeypatch.setattr(image_color_service, "_fetch_image", fetch)

    listed = client.get("/likes", headers=user.headers)

    assert listed.status_code == 200
    assert [i["image"]["color"] for i in listed.json()["data"]["items"]] == [
        None,
        None,
    ]
    assert downloads == []
    assert {key: _row(admin, key)[0] for key in before} == before

    awaiting = _read_scope(admin, in_progress, failed)
    assert awaiting.in_progress == {in_progress}
    assert awaiting.pending == {}
    assert awaiting.awaiting_colors

    settled = _read_scope(admin, failed)
    assert settled.in_progress == set()
    assert not settled.awaiting_colors


def test_a_route_recomputes_an_abandoned_key(
    client, make_user, track_payload, admin, monkeypatch
):
    user = make_user()
    key = _key()
    _like_without_computing(client, user, track_payload, [key])
    admin.table("image_colors").insert(
        {"image_key": key, "attempted_at": _ago(60)}
    ).execute()
    downloads = []

    def fetch(url):
        downloads.append(url)
        return _red_png()

    monkeypatch.setattr(image_color_service, "_fetch_image", fetch)

    client.get("/likes", headers=user.headers)
    second = client.get("/likes", headers=user.headers)

    assert downloads == [f"{key}=w192-h192-p-l90-rj"]
    row = _row(admin, key)[0]
    assert row["color"] == "#ee0000"
    assert row["failed_at"] is None
    assert second.json()["data"]["items"][0]["image"]["color"] == "#ee0000"
