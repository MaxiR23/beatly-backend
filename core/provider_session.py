# INFO: HTTP session of the external provider client: per-call (connect, read)
# timeouts, a per-request budget shared by every provider call, and the slow
# request log.

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import requests

from core.config import settings

logger = logging.getLogger(__name__)


def _now() -> float:
    # Looked up at call time (not captured at import) so a test can replace
    # it without touching time.monotonic, which asyncio and anyio use.
    return time.monotonic()


class _Budget:
    # Mutable holder of the deadline. The ContextVar stores the holder, not
    # the float, on purpose: every copy of the request's context (the child
    # task Starlette sends a StreamingResponse body from when spec_version <
    # 2.4, which is what uvicorn sends; the threadpool copy) shares the same
    # object, so clearing it is seen by all of them. A set(None) in a child
    # task would change that task's context only.
    __slots__ = ("deadline",)

    def __init__(self, deadline: float | None) -> None:
        self.deadline = deadline


# Holder of the absolute instant, on the _now clock, at which the budget of
# the incoming request runs out. None outside a request (scripts, tests
# without the app); its deadline is None once cleared.
_deadline: ContextVar[_Budget | None] = ContextVar("provider_deadline", default=None)


def _current_deadline() -> float | None:
    holder = _deadline.get()
    return None if holder is None else holder.deadline


@contextmanager
def provider_deadline(budget: float) -> Iterator[None]:
    token = _deadline.set(_Budget(_now() + budget))
    try:
        yield
    finally:
        _deadline.reset(token)


def clear_provider_deadline() -> None:
    # Clears the deadline of the holder of the current request. It mutates
    # the shared holder, so every copy of the request's context sees it,
    # including the parent task that runs the background tasks when the body
    # went out from a child task. The budget middleware calls it at the first
    # of three moments: the last body (or pathsend) goes out, the client's
    # http.disconnect comes through receive, or the app returns or raises.
    # A task that wants a budget arms its own with
    # provider_deadline(settings.provider_request_budget). A nested
    # provider_deadline has its own holder, so clearing inside it leaves the
    # outer one alone.
    holder = _deadline.get()
    if holder is not None:
        holder.deadline = None


class ProviderDeadlineExceeded(requests.exceptions.Timeout):
    # A Timeout on purpose: translate_upstream_errors() already turns it into
    # the 504 upstream_timeout. It is not an AppError because the network
    # layer must not know status codes, and it is not in PROVIDER_ERRORS
    # because those go to 502.
    pass


def _clipped(connect: float, read: float, remaining: float) -> tuple[float, float]:
    # The SUM of the two phases never exceeds what is left of the budget, as
    # in ADR 018. Clipping each phase alone with min(phase, remaining) would
    # not fit: a call starting with R left could end at
    # deadline - R + min(connect, R) + min(read, R), whose maximum is
    # deadline + connect = 12 + 3.05 = 15.05 s, over the app's 15 s.
    # Not the proportional scaling of ADR 018 either: connect + read = 13.05
    # exceeds 12, so it would always scale and the first call of every request
    # would get connect = 2.80 s, under the 3 s the 3.05 exists for. Here
    # connect keeps its 3.05 s while 6.1 s or more are left; below that the
    # two phases split the time half and half. Both stay > 0 for any
    # remaining > 0 (urllib3 treats a read timeout of 0 as already expired).
    c = min(connect, remaining / 2)
    return c, min(read, remaining - c)


class ProviderSession(requests.Session):
    def __init__(
        self,
        *,
        connect_timeout: float,
        read_timeout: float,
        slow_threshold: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._slow_threshold = slow_threshold
        self._clock = clock

    def _now(self) -> float:
        return self._clock() if self._clock is not None else _now()

    # No *args: the library passes everything by name, and a positional
    # argument fails loudly with TypeError instead of overriding the timeout.
    def request(self, method, url, **kwargs):
        start = self._now()
        try:
            return self._bounded(method, url, kwargs)
        finally:
            duration = self._now() - start
            if duration > self._slow_threshold:
                # Only method and path: never the query string, headers or
                # body (the search text and the ids travel there).
                logger.warning(
                    "provider_slow method=%s path=%s duration_ms=%d",
                    method.upper(),
                    urlsplit(url).path or "/",
                    round(duration * 1000),
                )

    def _bounded(self, method, url, kwargs):
        deadline = _current_deadline()
        if deadline is None:
            timeout = (self._connect_timeout, self._read_timeout)
        else:
            remaining = deadline - self._now()
            if remaining <= 0:
                raise ProviderDeadlineExceeded()
            timeout = _clipped(self._connect_timeout, self._read_timeout, remaining)

        # Assigned, not setdefault: our cap wins over any timeout a future
        # version of the library passes.
        kwargs["timeout"] = timeout
        return super().request(method, url, **kwargs)


def build_provider_session() -> ProviderSession:
    # Read per call, not at import, so tests that patch settings see it.
    return ProviderSession(
        connect_timeout=settings.provider_connect_timeout,
        read_timeout=settings.provider_read_timeout,
        slow_threshold=settings.provider_slow_request_threshold,
    )
