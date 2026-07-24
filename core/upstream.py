# INFO: Translates database and model-building failures into upstream errors.

# Services call this instead of writing their own try/except. Keeping the
# translation in one place is why every endpoint returns the same status
# for the same class of failure: earlier, when each function handled its
# own errors, three review findings came from one endpoint covering a
# case the others did not.
#
# Domain exceptions (NotFound, ResourceEmpty) are deliberately not in the
# caught set, so raising them inside the block passes straight through.
#
# SEE: https://docs.python.org/3/library/contextlib.html#contextlib.contextmanager

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
