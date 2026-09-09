# INFO: External search provider client shared across services.

import logging
from functools import lru_cache

from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError

# The first module-level logger outside app.py. setup_logging() is called
# once, in app.py, and is not called again here.
logger = logging.getLogger(__name__)

# Neutral alias so services and routers can annotate `provider: SearchProvider`
# without naming the provider by its brand, the same way they annotate `db: Client`.
SearchProvider = YTMusic


class ProviderParseError(Exception):
    # The provider answered 200, but with a layout its parsing library no
    # longer recognizes: it is the provider's problem, not a bug on our side.
    pass


class ProviderResourceMissing(Exception):
    # The provider answered, in a structured field, that there is nothing
    # behind this id at all -- unlike ProviderParseError, this is not a
    # provider failure, so it is deliberately kept OUT of PROVIDER_ERRORS:
    # translate_upstream_errors() must not turn it into a 502. The service
    # that knows which domain resource was requested translates it into a
    # 404 instead.
    pass


# The server-error class (an HTTP status >= 400 from the provider's backend)
# and the parse-error class above (an unparseable 200) are both the
# provider's problem, not ours, so both belong here. The library's own
# usage-error class is deliberately excluded: that one means we called the
# library wrong, which is a bug on our side and belongs to 500
# internal_error, not a 502 that would invite the client to retry.
# ProviderResourceMissing is deliberately excluded too, for the opposite
# reason: it is not a provider failure at all, see its docstring.
PROVIDER_ERRORS: tuple[type[Exception], ...] = (YTMusicServerError, ProviderParseError)

# get_song's playabilityStatus.status value that means "no track exists for
# this id". Deliberately not `!= "OK"`: UNPLAYABLE is a track that exists
# and whose /upnext, /lyrics and /related endpoints all work (measured on
# jfKfPfyJRdk, 5qap5aO4i9A, aircAruvnKk); treating it as missing would 404
# on good tracks.
_TRACK_MISSING_STATUS = "ERROR"


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


def _raise_if_track_missing(
    provider: SearchProvider, video_id: str, cause: Exception
) -> None:
    # Called only after the real operation already failed, so the happy
    # path never pays for this extra call. Indexed, not a .get() chain: if
    # playabilityStatus ever disappears that is a layout change and the
    # resulting KeyError has to be a 502, not a default 404 or a 500.
    try:
        song = provider.get_song(video_id)
    except (ValueError, IndexError) as exc:
        raise ProviderParseError("unparseable provider response") from exc

    if song["playabilityStatus"]["status"] == _TRACK_MISSING_STATUS:
        # from cause keeps the original failure in the chained log.
        raise ProviderResourceMissing(video_id) from cause


def provider_get_watch_playlist(
    provider: SearchProvider, video_id: str, *, limit: int
) -> dict:
    # The single call site into the library, so a layout change it can no
    # longer parse is translated into our own, translatable exception
    # instead of leaking ValueError/IndexError past core/upstream.py.
    try:
        return provider.get_watch_playlist(videoId=video_id, limit=limit)
    except (ValueError, IndexError) as exc:
        raise ProviderParseError("unparseable provider response") from exc
    except YTMusicServerError as exc:
        _raise_if_track_missing(provider, video_id, exc)
        # The probe did not confirm the track is missing: re-raise the
        # original failure so it still ends up as 502/504, never masked
        # by the probe's own outcome.
        raise


def provider_get_lyrics(provider: SearchProvider, browse_id: str) -> dict | None:
    # No probe here: if the watch playlist already answered, the track
    # exists. The single call site into the library, so a layout change it
    # can no longer parse is translated into our own, translatable
    # exception instead of leaking ValueError/IndexError past
    # core/upstream.py.
    try:
        return provider.get_lyrics(browse_id, timestamps=True)
    except (ValueError, IndexError) as exc:
        raise ProviderParseError("unparseable provider response") from exc


def provider_get_song_related(provider: SearchProvider, browse_id: str) -> list[dict]:
    # No probe here: if the watch playlist already answered, the track
    # exists. The single call site into the library, so a layout change it
    # can no longer parse is translated into our own, translatable
    # exception instead of leaking ValueError/IndexError past
    # core/upstream.py.
    try:
        return provider.get_song_related(browse_id)
    except (ValueError, IndexError) as exc:
        raise ProviderParseError("unparseable provider response") from exc


# The literal prefix the library itself requires for a credits browseId
# (get_song_credits rejects anything that does not start with it); the rest
# is the audio track's own id, unlike /lyrics and /related whose browse ids
# come out of the watch playlist. Verified live twice: the id of a music
# video for the same song builds a browseId that points at a different page.
_CREDITS_BROWSE_PREFIX = "MPTC"


def provider_get_song_credits(provider: SearchProvider, video_id: str) -> dict | None:
    browse_id = _CREDITS_BROWSE_PREFIX + video_id
    try:
        return provider.get_song_credits(browse_id)
    # ValueError is kept out of the (ValueError, IndexError) tuple every
    # other wrapper uses on purpose: here IndexError has a meaning of its
    # own and cannot be folded into the generic parse error. CREDITS_SECTIONS
    # starts with ["onResponseReceivedActions", 0, ...], so an empty list at
    # that point raises IndexError and a missing key raises KeyError -- both
    # are the same situation, the page carries no credits dialog. This is
    # the one deliberate divergence from the other six wrappers; do not
    # unify it back into a single except tuple.
    except ValueError as exc:
        raise ProviderParseError("unparseable provider response") from exc
    except (KeyError, IndexError) as exc:
        # The navigation branch does not return empty blindly: the probe
        # runs first. If the provider answers playabilityStatus.status ==
        # "ERROR", _raise_if_track_missing raises ProviderResourceMissing
        # and this becomes a 404. If the probe itself fails
        # (YTMusicServerError, a network timeout, ProviderParseError, or a
        # KeyError from a missing playabilityStatus), that exception
        # propagates and the endpoint is 502/504. The `return None` below is
        # only reached once the provider has positively confirmed, in a
        # structured field, that the track exists: this is not an except
        # that swallows an error and returns empty, it is an empty with
        # proof. None means "the provider has no credits for this track",
        # the same dict | None contract provider_get_lyrics already uses.
        _raise_if_track_missing(provider, video_id, exc)
        # Only the exception's type, never its message: the type is enough
        # to see the pattern in the rate of this line without leaking the
        # provider's message to the log.
        logger.info(
            "credits navigation failure on confirmed track %s: %s",
            video_id,
            type(exc).__name__,
        )
        return None
    except YTMusicServerError as exc:
        # Identical to provider_get_watch_playlist: probe, and re-raise the
        # original failure if the probe did not confirm the track is
        # missing, so a genuine failure still ends up as 502/504.
        _raise_if_track_missing(provider, video_id, exc)
        raise
    # The browseId is always built, never sourced from a watch playlist
    # response, so it is always truthy and always starts with "MPTC": the
    # library's YTMusicUserError for a malformed browseId can never fire
    # from this call site, unlike /lyrics and /related.
