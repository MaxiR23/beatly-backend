# test/core/test_provider_session.py
#
# Tests for the external provider session: per-call (connect, read) timeouts,
# the per-request budget and the slow request log.
#
# Tested:
# - Without a deadline the adapter receives the (connect, read) tuple and
#   stream is not forced; the library's own requests (visitor id GET, search POST) all
#   go through the bounded session
# - A connect or a read timeout propagates as the same object and is
#   translated into UpstreamTimeout
# - A request over the slow threshold logs exactly one provider_slow warning
#   with the method, the path and the duration in ms, also when it ends in an
#   exception; the query string, the body and the headers never appear; at or
#   under the threshold nothing is logged
# - _clipped keeps connect + read within what is left of the budget
# - Under a deadline the adapter receives the clipped tuple
# - An exhausted deadline raises ProviderDeadlineExceeded (a Timeout, so a
#   504) without calling the adapter
# - A call that ends past the deadline returns its response; the next one is
#   not made
# - clear_provider_deadline leaves no deadline and the enclosing one is
#   restored
# - Outside a request there is no deadline, and provider_deadline restores it
# - build_provider_session reads the four settings
#
# What is covered:
# - core/provider_session.py, against a fake adapter
#
# Run with: pytest test/core/test_provider_session.py -v
#
# SEE: core/provider_session.py, docs/adr/024-provider-requests-are-bounded-per-call-and-per-request.md

import logging

import pytest
import requests

from core import provider_session
from core.config import settings
from core.exceptions import UpstreamTimeout
from core.provider_session import (
    ProviderDeadlineExceeded,
    ProviderSession,
    _clipped,
    build_provider_session,
    clear_provider_deadline,
    provider_deadline,
)
from core.search_provider import SearchProvider
from core.upstream import translate_upstream_errors
from test.fake_provider_http import (
    BASE_URL,
    FakeAdapter,
    FakeClock,
    body_response,
)

_URL = f"{BASE_URL}/v1/search"


def _session(adapter, clock=None, **overrides):
    values = {"connect_timeout": 3.05, "read_timeout": 10.0, "slow_threshold": 2.0}
    session = ProviderSession(clock=clock, **{**values, **overrides})
    session.mount("https://", adapter)
    return session


def _slow_records(caplog):
    return [r for r in caplog.records if r.name == "core.provider_session"]


def test_request_without_a_deadline_sends_the_connect_read_tuple():
    adapter = FakeAdapter()

    response = _session(adapter, FakeClock()).post(_URL, json={})

    assert response.status_code == 200
    assert adapter.calls[0]["timeout"] == (3.05, 10.0)
    assert adapter.calls[0]["stream"] is False


def test_the_library_sends_every_request_through_the_bounded_session():
    adapter = FakeAdapter(lambda request: body_response(request, b"<html></html>"))
    session = _session(adapter, FakeClock())
    client = SearchProvider(requests_session=session)

    with pytest.raises((ValueError, KeyError, IndexError, TypeError)):
        client.search("anything", filter="songs")

    assert len(adapter.calls) >= 2
    assert all(call["timeout"] == (3.05, 10.0) for call in adapter.calls)


@pytest.mark.parametrize(
    "error", [requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout]
)
def test_timeouts_propagate_unchanged_and_translate_to_upstream_timeout(error):
    raised = error()

    def _boom(request):
        raise raised

    session = _session(FakeAdapter(_boom), FakeClock())

    with pytest.raises(error) as exc_info:
        session.post(_URL, json={})
    assert exc_info.value is raised

    with pytest.raises(UpstreamTimeout), translate_upstream_errors():
        session.post(_URL, json={})


def test_slow_request_logs_one_warning_with_method_path_and_duration(caplog):
    clock = FakeClock()
    adapter = FakeAdapter(
        lambda request: (clock.advance(2.5), body_response(request))[1]
    )

    with caplog.at_level(logging.DEBUG):
        _session(adapter, clock).post(_URL, json={})

    records = _slow_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert (
        records[0].getMessage()
        == "provider_slow method=POST path=/v1/search duration_ms=2500"
    )


def test_slow_request_log_carries_no_query_body_or_headers(caplog):
    clock = FakeClock()
    adapter = FakeAdapter(lambda request: (clock.advance(3), body_response(request))[1])

    with caplog.at_level(logging.DEBUG):
        _session(adapter, clock).post(
            f"{_URL}?q=secret-term",
            json={"user": "user-id-123"},
            headers={"Authorization": "Bearer secret-token"},
        )

    record = _slow_records(caplog)[0]
    text = record.getMessage() + repr(record.args)
    for secret in ("secret-term", "user-id-123", "secret-token", "?"):
        assert secret not in text


@pytest.mark.parametrize("seconds", [1.0, 2.0])
def test_request_at_or_under_the_threshold_logs_nothing(seconds, caplog):
    clock = FakeClock()
    adapter = FakeAdapter(
        lambda request: (clock.advance(seconds), body_response(request))[1]
    )

    with caplog.at_level(logging.DEBUG):
        _session(adapter, clock).post(_URL, json={})

    assert _slow_records(caplog) == []


def test_slow_request_ending_in_an_exception_logs_and_reraises_the_same_exception(
    caplog,
):
    clock = FakeClock()
    raised = requests.exceptions.ReadTimeout()

    def _slow_boom(request):
        clock.advance(5)
        raise raised

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(requests.exceptions.ReadTimeout) as exc_info,
    ):
        _session(FakeAdapter(_slow_boom), clock).post(_URL, json={})

    assert exc_info.value is raised
    records = _slow_records(caplog)
    assert len(records) == 1
    assert records[0].getMessage().endswith("duration_ms=5000")


@pytest.mark.parametrize("remaining", [12.0, 8.0, 4.0, 0.001])
def test_clipped_keeps_the_sum_within_the_remaining_budget(remaining):
    connect, read = _clipped(3.05, 10.0, remaining)

    assert connect + read <= remaining + 1e-9
    assert connect <= min(3.05, remaining)
    assert read <= min(10.0, remaining)
    assert connect > 0
    assert read > 0


def test_clipped_known_values():
    assert _clipped(3.05, 10.0, 12.0) == (3.05, pytest.approx(8.95))
    assert _clipped(3.05, 10.0, 8.0) == (3.05, pytest.approx(4.95))
    assert _clipped(3.05, 10.0, 4.0) == (2.0, 2.0)


def test_request_under_a_deadline_sends_the_clipped_tuple(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(provider_session, "_now", clock)
    adapter = FakeAdapter()

    with provider_deadline(12):
        _session(adapter).post(_URL, json={})

    connect, read = adapter.calls[0]["timeout"]
    assert connect == 3.05
    assert read == pytest.approx(8.95)


def test_exhausted_deadline_raises_without_calling_the_adapter(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(provider_session, "_now", clock)
    adapter = FakeAdapter()
    session = _session(adapter)

    with provider_deadline(5):
        clock.advance(5)
        with pytest.raises(ProviderDeadlineExceeded) as exc_info:
            session.post(_URL, json={})
        with pytest.raises(UpstreamTimeout), translate_upstream_errors():
            session.post(_URL, json={})

    assert isinstance(exc_info.value, requests.exceptions.Timeout)
    assert adapter.calls == []


def test_call_ending_past_the_deadline_returns_its_response_and_the_next_one_is_not_made(
    monkeypatch,
):
    clock = FakeClock()
    monkeypatch.setattr(provider_session, "_now", clock)
    adapter = FakeAdapter(lambda request: (clock.advance(7), body_response(request))[1])
    session = _session(adapter)

    with provider_deadline(5):
        response = session.post(_URL, json={})
        with pytest.raises(ProviderDeadlineExceeded):
            session.post(_URL, json={})

    assert response.json() == {"ok": True}
    assert len(adapter.calls) == 1


def test_clear_provider_deadline_leaves_no_deadline_and_the_outer_one_is_restored(
    monkeypatch,
):
    monkeypatch.setattr(provider_session, "_now", FakeClock())
    adapter = FakeAdapter()
    session = _session(adapter)

    with provider_deadline(20):
        outer = provider_session._current_deadline()
        with provider_deadline(5):
            clear_provider_deadline()
            assert provider_session._current_deadline() is None
            session.post(_URL, json={})
        assert provider_session._current_deadline() == outer

    assert adapter.calls[0]["timeout"] == (3.05, 10.0)
    assert provider_session._current_deadline() is None

    with provider_deadline(5):
        clear_provider_deadline()
    assert provider_session._current_deadline() is None


def test_no_deadline_outside_a_request():
    assert provider_session._current_deadline() is None

    with provider_deadline(5):
        assert provider_session._current_deadline() is not None
    assert provider_session._current_deadline() is None

    with pytest.raises(RuntimeError), provider_deadline(5):
        raise RuntimeError("boom")
    assert provider_session._current_deadline() is None


def test_build_provider_session_reads_the_settings_values(monkeypatch):
    monkeypatch.setattr(settings, "provider_connect_timeout", 1.5)
    monkeypatch.setattr(settings, "provider_read_timeout", 2.5)
    monkeypatch.setattr(settings, "provider_slow_request_threshold", 0.5)
    adapter = FakeAdapter()

    session = build_provider_session()
    session.mount("https://", adapter)
    session.post(_URL, json={})

    assert adapter.calls[0]["timeout"] == (1.5, 2.5)
    assert session._slow_threshold == 0.5
