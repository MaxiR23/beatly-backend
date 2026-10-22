# INFO: Transport of both Supabase clients: per-phase timeouts, one retry of a
# failed GET within a request budget, and the slow request log.

import logging
import time
from collections.abc import Callable

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# Errors that mean "the request may never have been served" and are worth one
# more try on a fresh connection. Left out on purpose: PoolTimeout (no free
# connection: retrying only waits again), WriteError/WriteTimeout (the request
# may have been partly sent), ProxyError and DecodingError (a retry returns the
# same thing). Only GET is retried at all (see handle_request).
_RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)

_PHASES = ("connect", "read", "write", "pool")


def _clipped_timeouts(timeouts: dict, remaining: float) -> dict:
    # Scales the four phases by the same factor so their SUM fits in what is
    # left of the budget. Over HTTPS an attempt can still last `connect` more
    # (TCP and TLS each get it), see ADR 018. Clipping each phase on its own
    # with min(phase, remaining) would not fit: the sum of four phases can far
    # exceed the remaining budget.
    total = sum(timeouts[phase] for phase in _PHASES)
    factor = min(1.0, remaining / total)
    return {phase: timeouts[phase] * factor for phase in _PHASES}


class SupabaseTransport(httpx.BaseTransport):
    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        request_budget: float,
        slow_threshold: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._request_budget = request_budget
        self._slow_threshold = slow_threshold
        self._clock = clock

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        start = self._clock()
        retried = False
        try:
            try:
                return self._attempt(request)
            except _RETRYABLE:
                remaining = self._request_budget - (self._clock() - start)
                if request.method != "GET" or remaining <= 0:
                    raise
                retried = True
                request.extensions = {
                    **request.extensions,
                    "timeout": _clipped_timeouts(
                        request.extensions["timeout"], remaining
                    ),
                }
                return self._attempt(request)
        finally:
            duration = self._clock() - start
            if duration > self._slow_threshold:
                # Only method and path: no query string, headers or body
                # (they carry user ids, tokens and payloads).
                logger.warning(
                    "supabase_slow method=%s path=%s duration_ms=%d retried=%s",
                    request.method,
                    request.url.path,
                    round(duration * 1000),
                    retried,
                )

    def _attempt(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        # httpx reads the body after handle_request returns, so a read error
        # in the middle of the body would escape the retry. Reading it here
        # keeps it inside the attempt. Side effect: response.elapsed is never
        # set; see ADR 018.
        try:
            response.read()
        except BaseException:
            response.close()
            raise
        return response

    def close(self) -> None:
        self._inner.close()


def build_supabase_http_client(*, http2: bool) -> httpx.Client:
    # Read per call, not at import, so tests that patch settings see it.
    timeout = httpx.Timeout(
        connect=settings.supabase_connect_timeout,
        read=settings.supabase_read_timeout,
        write=settings.supabase_write_timeout,
        pool=settings.supabase_pool_timeout,
    )
    # With transport= httpx ignores the Client's http2, limits and verify
    # (and stops reading proxies from the environment), so the inner
    # transport is built with http2 explicitly; HTTPTransport takes the same
    # default limits (100/20) the clients had.
    transport = SupabaseTransport(
        httpx.HTTPTransport(http2=http2),
        request_budget=settings.supabase_request_budget,
        slow_threshold=settings.supabase_slow_request_threshold,
    )
    return httpx.Client(timeout=timeout, follow_redirects=True, transport=transport)
