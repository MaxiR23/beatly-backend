# INFO: Translates database and model-building failures into upstream errors.

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from postgrest.exceptions import APIError
from pydantic import ValidationError

from core.exceptions import UpstreamError, UpstreamTimeout


@contextmanager
def translate_upstream_errors() -> Iterator[None]:
    try:
        yield
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (
        APIError,
        httpx.TransportError,
        ValidationError,
        TypeError,
        KeyError,
    ) as exc:
        raise UpstreamError() from exc
