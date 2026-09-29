# INFO: Cache-Control header values, the one place that knows the header.

from fastapi import Response

NO_STORE = "no-store"
PRIVATE_NO_CACHE = "private, no-cache"

_HEADER = "Cache-Control"


def set_max_age(response: Response, remaining: int | None) -> None:
    # No `private` or `public` on purpose: Authorization already keeps
    # shared caches from storing the token-protected endpoints, and on the
    # public share endpoints an intermediary caching is desirable.
    response.headers[_HEADER] = (
        f"max-age={remaining}" if remaining is not None else NO_STORE
    )


def private_no_cache(response: Response) -> None:
    # Mounted with dependencies= on the user-data routers. On an error the
    # handlers in app.py build a new response and this header is dropped,
    # which is why errors go out as no-store.
    response.headers[_HEADER] = PRIVATE_NO_CACHE
