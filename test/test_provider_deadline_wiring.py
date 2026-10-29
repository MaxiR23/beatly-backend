# test/test_provider_deadline_wiring.py
#
# Guards the wiring of the middleware that starts the provider budget, on the
# real app.
#
# Tested:
# - ProviderTimeBudget is the innermost middleware of the app, so no
#   other middleware's task sits between it and the route
# - A background task that runs after the budget of the request ran out arms
#   its own budget and calls the provider
# - A background task that does not arm one inherits no deadline: it sees
#   None and the call goes out with the (connect, read) tuple
# - A StreamingResponse under spec_version 2.3 (what uvicorn sends, the body
#   goes out from a child task): the background sees no deadline, also after
#   arming and leaving its own budget
# - The chunks of a StreamingResponse still see the deadline (more_body does
#   not clear it)
# - A FileResponse served with the pathsend extension clears it too
# - A client that disconnects mid-stream under spec_version 2.3 (the last
#   body never goes out): the background and the teardown of a request-scope
#   dependency with yield see no deadline
# - A task that copied the context (create_task) in a request that ends in a
#   500 sees no deadline once the request is over
# - A receive that delivers a request body (http.request) does not clear the
#   deadline: the route still sees it
#
# What is covered:
# - The order of app.user_middleware and the lifecycle of the deadline across
#   a sync route and its background tasks, called straight on the ASGI app for
#   the streaming and file cases. The async routes also go straight to the
#   ASGI app: the one that raises a 500, the one that disconnects mid-stream
#   and the POST with a body. That a route in the threadpool
#   sees the deadline is covered end to end by the budget tests in
#   test/routes/test_search.py
# - Not covered, on purpose: the teardowns of dependencies with yield that run
#   while the request is still in progress (scope="function", or a request
#   scope one when the route or the stream generator raises). They run inside
#   the app before any ASGI message passes, so the middleware cannot clear
#   them, and the client is still waiting, so seeing the deadline is right
#   (ADR 024)
#
# Run with: pytest test/test_provider_deadline_wiring.py -v
#
# SEE: app.py, core/provider_session.py, docs/adr/024-provider-requests-are-bounded-per-call-and-per-request.md

import asyncio
import json

import pytest
from fastapi import BackgroundTasks, Depends
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app import ProviderTimeBudget, app
from core import provider_session
from core.config import settings
from core.provider_session import (
    ProviderDeadlineExceeded,
    ProviderSession,
    provider_deadline,
)
from test.fake_provider_http import BASE_URL, FakeAdapter, FakeClock

_URL = f"{BASE_URL}/v1/search"

# What each piece of the test route saw, cleared by the tests before the call.
_seen: dict = {}
_clock = FakeClock()


def _call(name: str, adapter: FakeAdapter) -> None:
    session = ProviderSession(
        connect_timeout=3.05, read_timeout=10.0, slow_threshold=60
    )
    session.mount("https://", adapter)
    try:
        session.post(_URL, json={})
    except ProviderDeadlineExceeded as exc:
        _seen[name] = type(exc).__name__
        return
    _seen[name] = adapter.calls[-1]["timeout"]


def _task_with_its_own_budget() -> None:
    adapter = FakeAdapter()
    with provider_deadline(settings.provider_request_budget):
        _call("own_budget", adapter)
    _seen["own_budget_requests"] = len(adapter.calls)


def _task_without_a_budget() -> None:
    adapter = FakeAdapter()
    _seen["no_budget_deadline"] = provider_session._current_deadline()
    _call("no_budget", adapter)
    _seen["no_budget_requests"] = len(adapter.calls)


@app.get("/_test/provider-budget/background")
def _background_route(background_tasks: BackgroundTasks) -> dict:
    _clock.advance(settings.provider_request_budget + 1)
    _call("route", FakeAdapter())
    background_tasks.add_task(_task_with_its_own_budget)
    background_tasks.add_task(_task_without_a_budget)
    return {"ok": True}


def test_provider_time_budget_is_the_innermost_middleware():
    assert app.user_middleware[-1].cls is ProviderTimeBudget


@pytest.fixture
def background_run(monkeypatch):
    _seen.clear()
    monkeypatch.setattr(provider_session, "_now", _clock)
    response = TestClient(app).get("/_test/provider-budget/background")
    return response


def test_a_background_task_after_the_request_budget_ran_out_calls_with_its_own_budget(
    background_run,
):
    assert background_run.status_code == 200
    assert _seen["route"] == "ProviderDeadlineExceeded"
    assert _seen["own_budget_requests"] == 1
    connect, read = _seen["own_budget"]
    assert connect == 3.05
    assert read == pytest.approx(8.95)


def test_a_background_task_never_inherits_the_request_deadline(background_run):
    assert background_run.status_code == 200
    assert _seen["no_budget_deadline"] is None
    assert _seen["no_budget_requests"] == 1
    assert _seen["no_budget"] == (3.05, 10.0)


def _record_background(name: str) -> None:
    _seen[name] = provider_session._current_deadline()


def _record_background_with_own_budget() -> None:
    with provider_deadline(settings.provider_request_budget):
        _seen["own_inside"] = provider_session._current_deadline()
    _seen["own_after"] = provider_session._current_deadline()


@app.get("/_test/provider-budget/stream")
def _stream_route() -> StreamingResponse:
    def gen():
        _seen["chunk_1"] = provider_session._current_deadline()
        yield b"one"
        _seen["chunk_2"] = provider_session._current_deadline()
        yield b"two"

    return StreamingResponse(
        gen(), background=BackgroundTask(_record_background, "background")
    )


@app.get("/_test/provider-budget/stream-own-budget")
def _stream_own_budget_route() -> StreamingResponse:
    return StreamingResponse(
        iter([b"one"]), background=BackgroundTask(_record_background_with_own_budget)
    )


@app.get("/_test/provider-budget/file")
def _file_route() -> FileResponse:
    return FileResponse(
        _seen["file_path"], background=BackgroundTask(_record_background, "background")
    )


async def _teardown_recording_dependency():
    try:
        yield
    finally:
        _seen["teardown"] = provider_session._current_deadline()


@app.get("/_test/provider-budget/stream-disconnect")
async def _stream_disconnect_route(
    _dep: None = Depends(_teardown_recording_dependency),
) -> StreamingResponse:
    async def gen():
        _seen["chunk_1"] = provider_session._current_deadline()
        yield b"one"
        # Never set: the client's disconnect cancels the stream here.
        await asyncio.Event().wait()
        yield b"two"

    return StreamingResponse(
        gen(), background=BackgroundTask(_record_background, "background")
    )


async def _record_after_release(release: asyncio.Event) -> None:
    await release.wait()
    _seen["task"] = provider_session._current_deadline()


@app.get("/_test/provider-budget/fire-and-fail")
async def _fire_and_fail_route() -> None:
    release = asyncio.Event()
    _seen["release"] = release
    _seen["task_handle"] = asyncio.create_task(_record_after_release(release))
    raise RuntimeError("boom")


class _Body(BaseModel):
    name: str


@app.post("/_test/provider-budget/with-body")
def _with_body_route(body: _Body) -> dict:
    _seen["route"] = provider_session._current_deadline()
    return {"name": body.name}


def _scope(
    path: str,
    *,
    method: str = "GET",
    spec_version: str | None = None,
    extensions: dict | None = None,
) -> dict:
    # Straight on the ASGI app: TestClient sends no "asgi" key (Starlette then
    # reads spec 2.0) and does not announce the pathsend extension.
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "root_path": "",
    }
    if method == "POST":
        scope["headers"].append((b"content-type", b"application/json"))
    if spec_version is not None:
        scope["asgi"] = {"version": "3.0", "spec_version": spec_version}
    if extensions is not None:
        scope["extensions"] = extensions
    return scope


def _idle_receive(first_chunk_sent: asyncio.Event):
    # The client never disconnects until the app cancels the wait.
    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    return receive


def _disconnect_after_first_chunk(first_chunk_sent: asyncio.Event):
    # The client leaves as soon as the first chunk of the stream went out.
    async def receive():
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}

    return receive


def _request_body_receive(body: bytes):
    def factory(first_chunk_sent: asyncio.Event):
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if messages:
                return messages.pop(0)
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        return receive

    return factory


async def _run_asgi(
    path: str,
    *,
    method: str = "GET",
    spec_version: str | None = None,
    extensions: dict | None = None,
    make_receive=_idle_receive,
) -> list[dict]:
    scope = _scope(
        path, method=method, spec_version=spec_version, extensions=extensions
    )
    sent: list[dict] = []
    first_chunk_sent = asyncio.Event()

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("more_body"):
            first_chunk_sent.set()

    await app(scope, make_receive(first_chunk_sent), send)
    return sent


def _call_asgi(
    path: str,
    *,
    method: str = "GET",
    spec_version: str | None = None,
    extensions: dict | None = None,
    make_receive=_idle_receive,
) -> list[dict]:
    return asyncio.run(
        _run_asgi(
            path,
            method=method,
            spec_version=spec_version,
            extensions=extensions,
            make_receive=make_receive,
        )
    )


@pytest.fixture
def fake_clock(monkeypatch):
    _seen.clear()
    monkeypatch.setattr(provider_session, "_now", _clock)


def test_a_streaming_background_under_spec_2_3_never_inherits_the_deadline(
    fake_clock,
):
    sent = _call_asgi("/_test/provider-budget/stream", spec_version="2.3")

    assert sent[-1]["type"] == "http.response.body"
    assert "background" in _seen
    assert _seen["background"] is None


def test_the_chunks_of_a_stream_still_see_the_deadline(fake_clock):
    _call_asgi("/_test/provider-budget/stream", spec_version="2.3")

    assert _seen["chunk_1"] is not None
    assert _seen["chunk_2"] is not None
    assert _seen["background"] is None


def test_a_streaming_background_with_its_own_budget_ends_with_no_deadline(
    fake_clock,
):
    _call_asgi("/_test/provider-budget/stream-own-budget", spec_version="2.3")

    assert _seen["own_inside"] is not None
    assert _seen["own_after"] is None


def test_a_streaming_background_never_inherits_the_deadline_under_test_client(
    fake_clock,
):
    response = TestClient(app).get("/_test/provider-budget/stream")

    assert response.status_code == 200
    assert _seen["background"] is None


def test_a_file_response_served_with_pathsend_clears_the_deadline(fake_clock, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("content")
    _seen["file_path"] = str(path)

    sent = _call_asgi(
        "/_test/provider-budget/file",
        spec_version="2.4",
        extensions={"http.response.pathsend": {}},
    )

    assert sent[-1]["type"] == "http.response.pathsend"
    assert _seen["background"] is None


def test_a_streaming_background_never_inherits_the_deadline_after_a_disconnect(
    fake_clock,
):
    sent = _call_asgi(
        "/_test/provider-budget/stream-disconnect",
        spec_version="2.3",
        make_receive=_disconnect_after_first_chunk,
    )

    # The stream was cut: the second chunk never went out. (The empty final
    # body the outer BaseHTTPMiddleware adds comes after the background, so
    # it says nothing about the clear.)
    bodies = [m["body"] for m in sent if m["type"] == "http.response.body"]
    assert bodies[0] == b"one"
    assert b"two" not in bodies
    assert _seen["chunk_1"] is not None
    assert "background" in _seen
    assert _seen["background"] is None


def test_the_teardown_of_a_request_dependency_after_a_disconnect_sees_no_deadline(
    fake_clock,
):
    _call_asgi(
        "/_test/provider-budget/stream-disconnect",
        spec_version="2.3",
        make_receive=_disconnect_after_first_chunk,
    )

    assert "teardown" in _seen
    assert _seen["teardown"] is None


def test_a_task_that_copied_the_context_sees_no_deadline_after_a_500(fake_clock):
    async def run() -> None:
        with pytest.raises(RuntimeError):
            await _run_asgi("/_test/provider-budget/fire-and-fail")
        _seen["release"].set()
        await _seen["task_handle"]

    asyncio.run(run())

    assert "task" in _seen
    assert _seen["task"] is None


def test_reading_the_request_body_does_not_clear_the_deadline(fake_clock):
    body = json.dumps({"name": "x"}).encode()

    sent = _call_asgi(
        "/_test/provider-budget/with-body",
        method="POST",
        spec_version="2.3",
        make_receive=_request_body_receive(body),
    )

    assert sent[0]["status"] == 200
    assert _seen["route"] is not None
