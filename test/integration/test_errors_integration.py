# test/integration/test_errors_integration.py
#
# Integration tests of the client error report endpoint against the local
# stack.
#
# Tested:
# - POST /errors/playback stores a row for the caller only, with
#   app_version (migration 040) and the resolution columns untouched
# - The dedup query skips only the same track and stage
# - The cap query cuts a user at 20 rows and leaves other users alone
#
# What is covered:
# - The select, count and insert of this route against the real schema,
#   through the service-role client (error_logs has RLS and no policies)
#
# Run with: pytest -m integration test/integration/test_errors_integration.py -v
#
# SEE: routes/errors.py, services/error_log_service.py,
#      db/migrations/040_error_logs_app_version.sql

from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration

_RESOLUTION_COLUMNS = [
    "itag",
    "mime_type",
    "bitrate",
    "audio_quality",
    "source",
    "client_name",
    "client_version",
    "visitor_data_used",
    "retried",
    "playability_status",
    "resolve_duration_ms",
    "cache_age_ms",
    "audio_url_host",
    "audio_url_expire",
    "audio_url_client",
    "audio_url_mirror",
    "adaptive_format_count",
    "audio_format_count",
    "audio_formats_without_url",
    "urls_withheld",
]


def _body(**overrides):
    return {
        "track_id": f"it-{uuid4().hex}",
        "platform": "android",
        "os_version": "14",
        "app_version": "1.2.3",
        "stage": "resolve",
        "error_code": "E_RESOLVE",
        "error_message": "could not resolve the stream",
        "http_status": 403,
        **overrides,
    }


def _rows(admin, user):
    return (
        admin.table("error_logs").select("*").eq("user_id", user.user_id).execute().data
    )


def test_post_playback_error_stores_a_row_for_the_caller_only(client, make_user, admin):
    user = make_user()
    other = make_user()
    body = _body()

    response = client.post("/errors/playback", json=body, headers=user.headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    rows = _rows(admin, user)
    assert len(rows) == 1
    row = rows[0]
    for key, value in body.items():
        assert row[key] == value
    assert row["user_id"] == user.user_id
    assert row["resolved"] is False
    assert all(row[column] is None for column in _RESOLUTION_COLUMNS)
    assert _rows(admin, other) == []


def test_post_playback_error_dedups_only_the_same_track_and_stage(
    client, make_user, admin
):
    user = make_user()
    body = _body()

    first = client.post("/errors/playback", json=body, headers=user.headers)
    repeated = client.post("/errors/playback", json=body, headers=user.headers)
    other_stage = client.post(
        "/errors/playback",
        json={**body, "stage": "playback"},
        headers=user.headers,
    )

    assert [r.status_code for r in (first, repeated, other_stage)] == [200] * 3
    assert len(_rows(admin, user)) == 2


def test_post_playback_error_cuts_at_20_rows_per_user(client, make_user, admin):
    user = make_user()
    other = make_user()

    for _ in range(20):
        response = client.post("/errors/playback", json=_body(), headers=user.headers)
        assert response.status_code == 200

    over = client.post("/errors/playback", json=_body(), headers=user.headers)
    assert over.status_code == 429
    assert over.json() == {"ok": False, "reason": "rate_limited"}
    assert len(_rows(admin, user)) == 20

    unaffected = client.post("/errors/playback", json=_body(), headers=other.headers)
    assert unaffected.status_code == 200
