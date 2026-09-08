# INFO: Translates database, model-building and external provider failures into upstream errors.

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

import json
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import requests
from postgrest.exceptions import APIError
from pydantic import ValidationError

from core.exceptions import UpstreamError, UpstreamTimeout
from core.search_provider import PROVIDER_ERRORS

_ERROR_CLASSES = (
    APIError,
    httpx.TransportError,
    ValidationError,
    TypeError,
    KeyError,
    requests.exceptions.RequestException,
    json.JSONDecodeError,
    *PROVIDER_ERRORS,
)


@contextmanager
def translate_upstream_errors() -> Iterator[None]:
    try:
        yield
    # requests.exceptions.ConnectTimeout inherits from both Timeout and
    # ConnectionError, so this branch has to come first: otherwise the
    # broader RequestException below would catch it as a 502 instead of a
    # 504, the same ordering already required by httpx.ConnectTimeout.
    except (httpx.TimeoutException, requests.exceptions.Timeout) as exc:
        raise UpstreamTimeout() from exc
    except _ERROR_CLASSES as exc:
        raise UpstreamError() from exc
