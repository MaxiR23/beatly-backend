# INFO: Cache-Control header values, the one place that knows the header.

import re

from fastapi import Response

NO_STORE = "no-store"
PRIVATE_NO_CACHE = "private, no-cache"

_HEADER = "Cache-Control"
_MAX_AGE = re.compile(r"^max-age=([0-9]+)$")


def set_max_age(response: Response, remaining: int | None) -> None:
    # No `private` or `public` on purpose: Authorization already keeps
    # shared caches from storing the token-protected endpoints, and on the
    # public share endpoints an intermediary caching is desirable. The one
    # exception is set_private_max_age below.
    response.headers[_HEADER] = (
        f"max-age={remaining}" if remaining is not None else NO_STORE
    )


def set_private_max_age(response: Response, remaining: int | None) -> None:
    # For /tracks/{id}/upnext, whose response depends on the caller's play
    # history. `private` and not `no-cache`: the front pins the suggestions
    # per source, and with `no-cache` it could ask for them again in the
    # middle of a source and the list would change, which is what front #84
    # fixed.
    response.headers[_HEADER] = (
        f"private, max-age={remaining}" if remaining is not None else NO_STORE
    )


def private_no_cache(response: Response) -> None:
    # Mounted with dependencies= on the user-data routers. On an error the
    # handlers in app.py build a new response and this header is dropped,
    # which is why errors go out as no-store.
    response.headers[_HEADER] = PRIVATE_NO_CACHE


def cap_max_age(response: Response, ceiling: int) -> None:
    # Called by core/image_color_dependency.py while a response still carries
    # a color to be computed. It rewrites only the plain max-age that
    # set_max_age writes, to min(N, ceiling). Everything else stays as it is:
    # no-store, private, no-cache, a missing header and also the private
    # max-age of /tracks/{id}/upnext, which the front pins per source
    # (front #84) and a short value would break.
    match = _MAX_AGE.match(response.headers.get(_HEADER, ""))
    if match is None:
        return
    response.headers[_HEADER] = f"max-age={min(int(match.group(1)), ceiling)}"
