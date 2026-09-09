# INFO: External search provider client shared across services.

from functools import lru_cache

from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError

# Neutral alias so services and routers can annotate `provider: SearchProvider`
# without naming the provider by its brand, the same way they annotate `db: Client`.
SearchProvider = YTMusic


class ProviderParseError(Exception):
    # The provider answered 200, but with a layout its parsing library no
    # longer recognizes: it is the provider's problem, not a bug on our side.
    pass


# The server-error class (an HTTP status >= 400 from the provider's backend)
# and the parse-error class above (an unparseable 200) are both the
# provider's problem, not ours, so both belong here. The library's own
# usage-error class is deliberately excluded: that one means we called the
# library wrong, which is a bug on our side and belongs to 500
# internal_error, not a 502 that would invite the client to retry.
PROVIDER_ERRORS: tuple[type[Exception], ...] = (YTMusicServerError, ProviderParseError)


@lru_cache
def _provider() -> SearchProvider:
    return SearchProvider()


def get_search_provider() -> SearchProvider:
    return _provider()


def provider_search(
    provider: SearchProvider, q: str, *, filter: str, limit: int | None = None
) -> list[dict]:
    # The single call site into the library, so a layout change it can no
    # longer parse is translated into our own, translatable exception
    # instead of leaking ValueError/IndexError past core/upstream.py.
    try:
        if limit is None:
            return provider.search(q, filter=filter)
        return provider.search(q, filter=filter, limit=limit)
    except (ValueError, IndexError) as exc:
        raise ProviderParseError("unparseable provider response") from exc


def provider_get_album(provider: SearchProvider, browse_id: str) -> dict:
    # The single call site into the library, so a layout change it can no
    # longer parse is translated into our own, translatable exception
    # instead of leaking ValueError/IndexError past core/upstream.py.
    try:
        return provider.get_album(browse_id)
    except (ValueError, IndexError) as exc:
        raise ProviderParseError("unparseable provider response") from exc


def provider_get_artist(provider: SearchProvider, channel_id: str) -> dict:
    # The single call site into the library, so a layout change it can no
    # longer parse is translated into our own, translatable exception
    # instead of leaking ValueError/IndexError past core/upstream.py.
    try:
        return provider.get_artist(channel_id)
    except (ValueError, IndexError) as exc:
        raise ProviderParseError("unparseable provider response") from exc
