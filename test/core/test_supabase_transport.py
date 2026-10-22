# test/core/test_supabase_transport.py
#
# Tests for the transport both Supabase clients share: the one retry of a
# failed GET, the request budget, and the slow request log.
#
# Tested:
# - A GET that fails with ConnectError, ConnectTimeout, ReadTimeout,
#   ReadError or RemoteProtocolError is retried once and the second
#   response is returned; so is a GET whose body read fails midway
# - A GET failing twice raises the second error, with no third attempt
# - POST, PATCH, PUT, DELETE and HEAD are never retried
# - PoolTimeout, WriteTimeout, WriteError, ProxyError and DecodingError are
#   not retried on a GET
# - A complete 503 or 520 response is returned without a retry
# - No retry when the budget is spent; the second attempt's timeouts are
#   scaled so their sum fits the remaining budget (and left alone when it
#   already does)
# - close() closes the inner transport
# - One supabase_slow warning with method, path, duration_ms and retried
#   (also when the request ends in an exception), nothing under the
#   threshold, and never the query string, headers or body
# - Through a real supabase client: a retried GET is a 200 for the route,
#   two timeouts are a 504 upstream_timeout, two other failures a 502
#   upstream_error
# - build_supabase_http_client applies the Settings timeouts and the
#   requested protocol
#
# What is covered:
# - core/supabase_transport.py, with a scripted httpx.BaseTransport: no
#   network and no database
#
# Run with: pytest test/core/test_supabase_transport.py -v
#
# SEE: core/supabase_transport.py, docs/adr/018

import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from supabase import ClientOptions, create_client

from app import app
from core.config import settings
from core.database import get_db
from core.supabase_transport import SupabaseTransport, build_supabase_http_client

_URL = "http://supabase.test/rest/v1/playlists"
_TIMEOUTS = {"connect": 2.0, "read": 4.0, "write": 2.0, "pool": 2.0}
_TIMEOUTS_SUM = sum(_TIMEOUTS.values())
_BUDGET = _TIMEOUTS_SUM
_RETRYABLE = [
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
]
_NOT_RETRYABLE = [
    httpx.PoolTimeout,
    httpx.WriteTimeout,
    httpx.WriteError,
    httpx.ProxyError,
    httpx.DecodingError,
]


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _FailingStream(httpx.SyncByteStream):
    def __init__(self):
        self.closed = False

    def __iter__(self):
        yield b"par"
        raise httpx.ReadError("body cut")

    def close(self):
        self.closed = True


class _ScriptedTransport(httpx.BaseTransport):
    # Each call takes the next result (an exception to raise or a response
    # to return) and advances the fake clock by the next duration.
    def __init__(self, results, clock=None, durations=()):
        self.results = list(results)
        self.clock = clock
        self.durations = list(durations)
        self.requests = []
        self.timeouts = []
        self.closed = False

    def handle_request(self, request):
        self.requests.append(request.method)
        self.timeouts.append(dict(request.extensions.get("timeout", {})))
        if self.clock is not None and self.durations:
            self.clock.now += self.durations.pop(0)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self):
        self.closed = True


def _request(method="GET", url=_URL, **kwargs):
    return httpx.Request(method, url, extensions={"timeout": dict(_TIMEOUTS)}, **kwargs)


def _ok(status=200, **kwargs):
    return httpx.Response(status, **kwargs)


def _transport(inner, clock=None, budget=_BUDGET, threshold=1.0):
    return SupabaseTransport(
        inner,
        request_budget=budget,
        slow_threshold=threshold,
        clock=clock or _Clock(),
    )


@pytest.mark.parametrize("error", _RETRYABLE)
def test_get_is_retried_once_after_a_transport_error(error):
    inner = _ScriptedTransport([error("boom"), _ok(200, content=b"second")])

    response = _transport(inner).handle_request(_request())

    assert inner.requests == ["GET", "GET"]
    assert response.status_code == 200
    assert response.content == b"second"


def test_get_body_read_error_is_retried():
    stream = _FailingStream()
    inner = _ScriptedTransport([_ok(200, stream=stream), _ok(200, content=b"whole")])

    response = _transport(inner).handle_request(_request())

    assert inner.requests == ["GET", "GET"]
    assert response.content == b"whole"
    assert stream.closed is True


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ReadError])
def test_get_that_fails_twice_raises_the_second_error_without_a_third_attempt(error):
    second = error("second")
    inner = _ScriptedTransport([error("first"), second, _ok()])

    with pytest.raises(error) as exc_info:
        _transport(inner).handle_request(_request())

    assert exc_info.value is second
    assert inner.requests == ["GET", "GET"]


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE", "HEAD"])
def test_non_get_is_never_retried(method, error):
    first = error("boom")
    inner = _ScriptedTransport([first, _ok()])

    with pytest.raises(error) as exc_info:
        _transport(inner).handle_request(_request(method))

    assert exc_info.value is first
    assert inner.requests == [method]


@pytest.mark.parametrize("error", _NOT_RETRYABLE)
def test_get_is_not_retried_for_errors_outside_the_set(error):
    inner = _ScriptedTransport([error("boom"), _ok()])

    with pytest.raises(error):
        _transport(inner).handle_request(_request())

    assert inner.requests == ["GET"]


@pytest.mark.parametrize("status", [503, 520])
def test_a_complete_503_response_is_not_retried(status):
    inner = _ScriptedTransport([_ok(status), _ok()])

    response = _transport(inner).handle_request(_request())

    assert response.status_code == status
    assert inner.requests == ["GET"]


def test_no_retry_when_the_budget_is_spent():
    clock = _Clock()
    first = httpx.ReadTimeout("slow")
    inner = _ScriptedTransport([first, _ok()], clock, durations=[_BUDGET])

    with pytest.raises(httpx.ReadTimeout) as exc_info:
        _transport(inner, clock).handle_request(_request())

    assert exc_info.value is first
    assert inner.requests == ["GET"]


def test_second_attempt_timeouts_are_cut_to_the_remaining_budget():
    clock = _Clock()
    inner = _ScriptedTransport(
        [httpx.ReadTimeout("slow"), _ok()], clock, durations=[4.0, 0.1]
    )

    _transport(inner, clock).handle_request(_request())

    factor = (_BUDGET - 4.0) / _TIMEOUTS_SUM
    assert inner.timeouts[0] == _TIMEOUTS
    assert inner.timeouts[1] == {
        phase: pytest.approx(value * factor) for phase, value in _TIMEOUTS.items()
    }
    assert sum(inner.timeouts[1].values()) == pytest.approx(_BUDGET - 4.0)


def test_second_attempt_keeps_full_timeouts_when_budget_exceeds_their_sum():
    clock = _Clock()
    inner = _ScriptedTransport(
        [httpx.ReadTimeout("slow"), _ok()], clock, durations=[4.0, 0.1]
    )

    _transport(inner, clock, budget=2 * _BUDGET).handle_request(_request())

    assert inner.timeouts[1] == _TIMEOUTS


def test_close_closes_the_inner_transport():
    inner = _ScriptedTransport([])

    _transport(inner).close()

    assert inner.closed is True


def _slow_records(caplog):
    return [r for r in caplog.records if r.name == "core.supabase_transport"]


def test_slow_request_logs_one_warning_with_the_four_fields(caplog):
    clock = _Clock()
    inner = _ScriptedTransport([_ok()], clock, durations=[1.5])

    with caplog.at_level(logging.DEBUG):
        _transport(inner, clock).handle_request(_request())

    records = _slow_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == (
        "supabase_slow method=GET path=/rest/v1/playlists duration_ms=1500 "
        "retried=False"
    )


def test_fast_request_logs_nothing(caplog):
    clock = _Clock()
    inner = _ScriptedTransport([_ok()], clock, durations=[0.2])

    with caplog.at_level(logging.DEBUG):
        _transport(inner, clock).handle_request(_request())

    assert _slow_records(caplog) == []


def test_retried_slow_request_logs_once_with_retried_true(caplog):
    clock = _Clock()
    inner = _ScriptedTransport(
        [httpx.ReadTimeout("slow"), _ok()], clock, durations=[2.0, 0.5]
    )

    with caplog.at_level(logging.DEBUG):
        _transport(inner, clock).handle_request(_request())

    records = _slow_records(caplog)
    assert len(records) == 1
    assert "duration_ms=2500" in records[0].getMessage()
    assert "retried=True" in records[0].getMessage()


def test_failed_request_is_still_logged(caplog):
    clock = _Clock()
    inner = _ScriptedTransport(
        [httpx.ReadTimeout("a"), httpx.ReadTimeout("b")], clock, durations=[1.0, 1.0]
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(httpx.ReadTimeout):
        _transport(inner, clock).handle_request(_request())

    records = _slow_records(caplog)
    assert len(records) == 1
    assert "retried=True" in records[0].getMessage()


def test_slow_log_carries_no_query_headers_or_body(caplog):
    user_id = "5f0e7a4c-0c3b-4b8e-9d54-0a1f2b3c4d5e"
    clock = _Clock()
    inner = _ScriptedTransport([_ok(), _ok()], clock, durations=[2.0, 2.0])
    get = _request(
        "GET",
        f"{_URL}?user_id=eq.{user_id}&select=id,title",
        headers={"Authorization": "Bearer secret-token"},
    )
    post = _request("POST", _URL, content=b'{"secret": "payload-value"}')

    with caplog.at_level(logging.DEBUG):
        transport = _transport(inner, clock)
        transport.handle_request(get)
        transport.handle_request(post)

    records = _slow_records(caplog)
    assert len(records) == 2
    for record in records:
        text = record.getMessage()
        for leaked in (
            "user_id",
            user_id,
            "select",
            "Bearer",
            "secret-token",
            "payload-value",
        ):
            assert leaked not in text


@pytest.fixture
def _db_override():
    yield
    app.dependency_overrides.pop(get_db, None)


def _db_over(results):
    inner = _ScriptedTransport(results)
    http_client = httpx.Client(
        transport=_transport(inner), timeout=httpx.Timeout(**_TIMEOUTS)
    )
    db = create_client(
        "http://supabase.test", "key", ClientOptions(httpx_client=http_client)
    )
    app.dependency_overrides[get_db] = lambda: db
    return inner


_GENRE_BODY = (
    b'[{"id": "g1", "slug": "rock", "name": "Rock", "description": null,'
    b' "sort_order": 1, "created_at": "2025-01-01T00:00:00Z"}]'
)
_JSON = {"content-type": "application/json"}


def test_route_answers_200_when_the_retry_succeeds(_db_override):
    inner = _db_over(
        [httpx.ReadError("cut"), _ok(200, content=_GENRE_BODY, headers=_JSON)]
    )

    response = TestClient(app, raise_server_exceptions=False).get("/genres")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["data"]["items"][0]["slug"] == "rock"
    assert inner.requests == ["GET", "GET"]


def test_route_answers_504_when_both_attempts_time_out(_db_override):
    inner = _db_over([httpx.ReadTimeout("a"), httpx.ReadTimeout("b")])

    response = TestClient(app, raise_server_exceptions=False).get("/genres")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    assert inner.requests == ["GET", "GET"]


def test_route_answers_502_when_both_attempts_fail(_db_override):
    inner = _db_over([httpx.ReadError("a"), httpx.ReadError("b")])

    response = TestClient(app, raise_server_exceptions=False).get("/genres")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert inner.requests == ["GET", "GET"]


@pytest.mark.parametrize("http2", [True, False])
def test_build_supabase_http_client_uses_the_settings_timeouts_and_protocol(http2):
    client = build_supabase_http_client(http2=http2)
    try:
        assert client.timeout == httpx.Timeout(
            connect=settings.supabase_connect_timeout,
            read=settings.supabase_read_timeout,
            write=settings.supabase_write_timeout,
            pool=settings.supabase_pool_timeout,
        )
        assert client.follow_redirects is True
        assert isinstance(client._transport, SupabaseTransport)
        # Private attribute of httpcore's ConnectionPool: no public API reads
        # the protocol before connecting. httpx/httpcore are pinned.
        assert client._transport._inner._pool._http2 is http2
    finally:
        client.close()
