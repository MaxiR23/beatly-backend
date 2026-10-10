# test/routes/test_errors.py
#
# Tests for the client error report endpoint.
#
# Tested:
# - POST /errors/playback stores a row for the caller: user_id comes from
#   the token, created_at from the same clock as the windows, and
#   http_status is null when omitted
# - A report for the same user, track and stage within 5 s answers 200
#   without inserting and without running the cap query; dedup wins over
#   the cap
# - Returns 429 rate_limited at 20 rows in 10 minutes, without inserting
# - The dedup and cap queries are scoped to the caller, and the windows
#   are 5 s and 10 min from created_at
# - Returns 422 invalid_request, without touching the database, for an
#   unknown platform or stage, unknown fields (user_id included), a
#   missing field, text over its limit, text empty after cleaning and an
#   http_status out of range
# - Text is stripped of control characters and trimmed before storing
# - An unknown track_id is accepted: never a 404
# - Returns 401 unauthorized without touching the database
# - Returns 502/504 on a failing dedup, count or insert, an insert with no
#   row and a missing count
# - Responses never repeat client values; the log escapes track_id and
#   never holds the message
# - Cache-Control: no-store on every response
#
# What is covered:
# - Happy path, dedup (the expected 200-without-write case), cap, invalid
#   input, unauthenticated, upstream failure and timeout. No parent
#   resource and no external service: the database is the only upstream.
#
# Database access is overridden through get_db (core/database.py).
#
# Run with: pytest test/routes/test_errors.py -v
#
# SEE: routes/errors.py, services/error_log_service.py, models/errors.py

import logging
from datetime import datetime
from unittest.mock import MagicMock, call

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.auth import get_current_user_id
from core.database import get_db

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_URL = "/errors/playback"
_SECRET = "secret-db-detail"

_BODY = {
    "track_id": "abc123",
    "platform": "ios",
    "os_version": "17.4",
    "app_version": "1.2.3",
    "stage": "playback",
    "error_code": "E_PLAYER",
    "error_message": "the stream stalled",
    "http_status": 403,
}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _use_db(dedup_data=None, count=0, insert_data=None):
    """One MagicMock, one chain per query (dedup, cap, insert)."""
    db = MagicMock()
    table = db.table.return_value
    dedup = table.select.return_value.eq.return_value.eq.return_value.eq.return_value
    dedup.gte.return_value.limit.return_value.execute.return_value = MagicMock(
        data=dedup_data or []
    )
    cap = table.select.return_value.eq.return_value.gte.return_value
    cap.limit.return_value.execute.return_value = MagicMock(data=[], count=count)
    table.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "row"}] if insert_data is None else insert_data
    )
    app.dependency_overrides[get_db] = lambda: db
    return db


def _dedup_chain(db):
    return db.table.return_value.select.return_value.eq.return_value


def _cap_chain(db):
    return db.table.return_value.select.return_value.eq.return_value


def _post(body=None, **kwargs):
    return client.post(_URL, json=_BODY if body is None else body, **kwargs)


def test_post_playback_error_stores_a_row_for_the_caller():
    _use_auth()
    db = _use_db()

    response = _post()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    db.table.assert_called_with("error_logs")
    inserted = db.table.return_value.insert.call_args.args[0]
    assert set(inserted) == {*_BODY, "user_id", "created_at"}
    assert inserted["user_id"] == _USER_ID
    assert {k: inserted[k] for k in _BODY} == _BODY


def test_post_playback_error_stores_null_http_status_when_omitted():
    _use_auth()
    db = _use_db()
    body = {k: v for k, v in _BODY.items() if k != "http_status"}

    response = _post(body)

    assert response.status_code == 200
    inserted = db.table.return_value.insert.call_args.args[0]
    assert inserted["http_status"] is None


def test_post_playback_error_within_5s_is_deduplicated_without_insert():
    _use_auth()
    db = _use_db(dedup_data=[{"id": "existing"}])

    response = _post()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    db.table.return_value.insert.assert_not_called()
    # Only the dedup select ran: the cap query never did.
    assert db.table.return_value.select.call_count == 1


def test_post_playback_error_dedup_wins_over_the_cap():
    _use_auth()
    db = _use_db(dedup_data=[{"id": "existing"}], count=20)

    response = _post()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    db.table.return_value.insert.assert_not_called()
    assert db.table.return_value.select.call_count == 1


def test_post_playback_error_dedup_query_is_scoped_to_user_track_and_stage():
    _use_auth()
    db = _use_db()

    _post()

    select = db.table.return_value.select
    assert select.call_args_list[0] == call("id")
    eq_calls = [
        c for c in select.return_value.eq.call_args_list if c.args[0] == "user_id"
    ]
    assert call("user_id", _USER_ID) in eq_calls
    first = select.return_value.eq.return_value
    assert first.eq.call_args_list == [call("track_id", "abc123")]
    assert first.eq.return_value.eq.call_args_list == [call("stage", "playback")]


def test_post_playback_error_cap_query_is_scoped_to_the_caller():
    _use_auth()
    db = _use_db()

    _post()

    select = db.table.return_value.select
    assert select.call_args_list[1] == call("id", count="exact")
    # The cap query is the second select, so its filter is the second eq
    # on the shared chain (the first one belongs to the dedup query).
    eq_calls = select.return_value.eq.call_args_list
    assert len(eq_calls) == 2
    assert eq_calls[1] == call("user_id", _USER_ID)


def test_post_playback_error_windows_are_5s_and_10min_from_created_at():
    _use_auth()
    db = _use_db()

    _post()

    first = db.table.return_value.select.return_value.eq.return_value
    dedup_since = first.eq.return_value.eq.return_value.gte.call_args.args[1]
    cap_since = first.gte.call_args.args[1]
    created_at = db.table.return_value.insert.call_args.args[0]["created_at"]
    created = datetime.fromisoformat(created_at)
    assert (created - datetime.fromisoformat(dedup_since)).total_seconds() == 5
    assert (created - datetime.fromisoformat(cap_since)).total_seconds() == 600


def test_post_playback_error_over_the_cap_returns_429_without_insert():
    _use_auth()
    db = _use_db(count=20)

    response = _post()

    assert response.status_code == 429
    assert response.json() == {"ok": False, "reason": "rate_limited"}
    db.table.return_value.insert.assert_not_called()


def test_post_playback_error_below_the_cap_inserts():
    _use_auth()
    db = _use_db(count=19)

    response = _post()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": None}
    db.table.return_value.insert.assert_called_once()


def test_post_playback_error_accepts_a_track_id_unknown_to_the_catalog():
    _use_auth()
    db = _use_db()

    response = _post({**_BODY, "track_id": "not-in-any-catalog"})

    assert response.status_code == 200
    assert {c.args[0] for c in db.table.call_args_list} == {"error_logs"}


def test_post_playback_error_without_token_returns_401():
    db = _use_db()

    response = _post()

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    db.table.assert_not_called()


def test_post_playback_error_with_invalid_token_returns_401():
    db = _use_db()

    response = _post(headers={"Authorization": "Bearer garbage"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    db.table.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        {"platform": "web"},
        {"platform": "IOS"},
        {"platform": " ios"},
        {"stage": "download"},
    ],
)
def test_post_playback_error_rejects_an_unknown_platform_or_stage(override):
    _use_auth()
    db = _use_db()

    response = _post({**_BODY, **override})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


@pytest.mark.parametrize("field", ["user_id", "resolved"])
def test_post_playback_error_rejects_unknown_fields(field):
    _use_auth()
    db = _use_db()

    response = _post({**_BODY, field: "x"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "track_id",
        "platform",
        "os_version",
        "app_version",
        "stage",
        "error_code",
        "error_message",
    ],
)
def test_post_playback_error_rejects_a_missing_required_field(field):
    _use_auth()
    db = _use_db()
    body = {k: v for k, v in _BODY.items() if k != field}

    response = _post(body)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


_LIMITS = {
    "track_id": 64,
    "os_version": 32,
    "app_version": 32,
    "error_code": 64,
    "error_message": 1000,
}


@pytest.mark.parametrize("field", list(_LIMITS))
def test_post_playback_error_rejects_text_over_its_limit(field):
    _use_auth()
    db = _use_db()

    response = _post({**_BODY, field: "x" * (_LIMITS[field] + 1)})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


@pytest.mark.parametrize("field", list(_LIMITS))
def test_post_playback_error_accepts_text_at_its_limit(field):
    _use_auth()
    db = _use_db()

    response = _post({**_BODY, field: "x" * _LIMITS[field]})

    assert response.status_code == 200
    inserted = db.table.return_value.insert.call_args.args[0]
    assert inserted[field] == "x" * _LIMITS[field]


@pytest.mark.parametrize("field", list(_LIMITS))
def test_post_playback_error_rejects_text_empty_after_cleaning(field):
    _use_auth()
    db = _use_db()

    response = _post({**_BODY, field: " \n\x00 "})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


@pytest.mark.parametrize("status", [99, 600])
def test_post_playback_error_rejects_http_status_out_of_range(status):
    _use_auth()
    db = _use_db()

    response = _post({**_BODY, "http_status": status})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_post_playback_error_strips_control_characters_and_trims():
    _use_auth()
    db = _use_db()

    response = _post({**_BODY, "error_message": "  boom\x00 at\nline  2  "})

    assert response.status_code == 200
    inserted = db.table.return_value.insert.call_args.args[0]
    assert inserted["error_message"] == "boom atline  2"


def test_post_playback_error_returns_502_when_the_dedup_query_fails():
    _use_auth()
    db = _use_db()
    _dedup = db.table.return_value.select.return_value.eq.return_value.eq.return_value
    _dedup.eq.return_value.gte.return_value.limit.return_value.execute.side_effect = (
        APIError({"message": _SECRET})
    )

    response = _post()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert _SECRET not in response.text


def test_post_playback_error_returns_502_when_the_cap_count_fails():
    _use_auth()
    db = _use_db()
    cap = db.table.return_value.select.return_value.eq.return_value.gte.return_value
    cap.limit.return_value.execute.side_effect = APIError({"message": _SECRET})

    response = _post()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert _SECRET not in response.text


def test_post_playback_error_returns_504_when_the_insert_times_out():
    _use_auth()
    db = _use_db()
    db.table.return_value.insert.return_value.execute.side_effect = httpx.ReadTimeout(
        _SECRET
    )

    response = _post()

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    assert _SECRET not in response.text


def test_post_playback_error_returns_502_when_the_insert_returns_no_row():
    _use_auth()
    _use_db(insert_data=[])

    response = _post()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("count", [None, True])
def test_post_playback_error_returns_502_when_the_count_is_missing(count):
    _use_auth()
    db = _use_db(count=count)

    response = _post()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    db.table.return_value.insert.assert_not_called()


_SENTINEL_TRACK = "sentinel-track-9f3a"
_SENTINEL_MESSAGE = "sentinel-message-7c1d"


@pytest.mark.parametrize("path", ["ok", "dedup", "invalid", "cap", "upstream"])
def test_post_playback_error_responses_never_echo_client_values(path):
    _use_auth()
    body = {
        **_BODY,
        "track_id": _SENTINEL_TRACK,
        "error_message": _SENTINEL_MESSAGE,
    }
    db = _use_db(
        dedup_data=[{"id": "x"}] if path == "dedup" else None,
        count=20 if path == "cap" else 0,
    )
    if path == "invalid":
        body["error_message"] = _SENTINEL_MESSAGE + "x" * 1000
    if path == "upstream":
        db.table.return_value.insert.return_value.execute.side_effect = APIError(
            {"message": _SENTINEL_MESSAGE}
        )

    response = _post(body)

    assert _SENTINEL_TRACK not in response.text
    assert _SENTINEL_MESSAGE not in response.text


def test_post_playback_error_logs_track_id_escaped_and_never_the_message(caplog):
    _use_auth()
    _use_db(dedup_data=[{"id": "x"}])
    track_id = "it's-a-track"
    message = "sentinel-message-in-log"

    with caplog.at_level(logging.INFO):
        response = _post({**_BODY, "track_id": track_id, "error_message": message})

    assert response.status_code == 200
    assert any(repr(track_id) in r.getMessage() for r in caplog.records)
    assert not any(message in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("path", [200, 401, 422, 429, 502])
def test_post_playback_error_sends_no_store(path):
    if path != 401:
        _use_auth()
    db = _use_db(count=20 if path == 429 else 0)
    if path == 502:
        db.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[]
        )
    body = {**_BODY, "stage": "bad"} if path == 422 else _BODY

    response = _post(body)

    assert response.status_code == path
    assert response.headers.get_list("cache-control") == ["no-store"]
