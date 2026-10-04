# INFO: Searches the external provider for artists, songs and albums.

import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from core.cache import CacheClient, cache_get, cache_set, cache_ttl, hashed_key
from core.exceptions import UpstreamError
from core.search_provider import SearchProvider, provider_search
from core.thumbnails import square_thumbnail_url
from core.upstream import translate_upstream_errors
from models.search import (
    SearchAlbum,
    SearchArtist,
    SearchResult,
    SearchSong,
)

logger = logging.getLogger(__name__)

_ItemT = TypeVar("_ItemT", SearchSong, SearchAlbum)
_ModelT = TypeVar("_ModelT", bound=BaseModel)

# TTL for a cached search result: rankings shift, so this is the shortest
# TTL of the eight cached provider operations.
_SEARCH_TTL_SECONDS = 60 * 60

# Side of the square image requested from the provider's CDN, with smart
# crop, for the artist image and the song and album covers. The rule is the
# one in core/thumbnails.py, so a larger URL is rewritten down too.
_THUMBNAIL_SIZE = 544


def search(
    provider: SearchProvider, cache: CacheClient, q: str
) -> tuple[SearchResult, int | None]:
    # The only one of the eight operations keyed by hashed_key(), not
    # cache_key(): q is free text typed by a user, the other seven ids are
    # opaque provider ids.
    key = hashed_key("search", q)
    # The read stays outside translate_upstream_errors(): a ValidationError
    # from a stale cached value is a cache failure, not a 502.
    cached = cache_get(cache, key, SearchResult)
    if cached is not None:
        return cached, cache_ttl(cache, key)

    result = _fetch_search(provider, q)
    cache_set(cache, key, result, ttl=_SEARCH_TTL_SECONDS)
    # A miss returns the full TTL even if cache_set failed: the data's age is 0.
    return result, _SEARCH_TTL_SECONDS


def _fetch_search(provider: SearchProvider, q: str) -> SearchResult:
    with translate_upstream_errors():
        artist_rows = provider_search(provider, q, filter="artists", limit=1)
        song_rows = provider_search(provider, q, filter="songs")
        album_rows = provider_search(provider, q, filter="albums")

        # Only the first row is the artist: a first row that is not an
        # object gives a null artist, it never falls through to the second.
        artist = _map_artist(_rows("artist", _first_row(artist_rows)))
        songs = [
            song
            for song in map(_map_song, _rows("song", song_rows))
            if song is not None
        ]
        albums = [
            album
            for album in map(_map_album, _rows("album", album_rows))
            if album is not None
        ]
        # Per list; the artist (limit 1) is excluded on purpose: a dropped
        # artist row only makes data.artist null.
        _require_survivors("song", song_rows, songs)
        _require_survivors("album", album_rows, albums)

    # Pure list reshuffling: no provider call and no model construction can
    # fail here, so it stays outside the translated block.
    return SearchResult(
        artist=artist,
        songs=_ordered_by_artist(songs, artist),
        albums=_ordered_by_artist(albums, artist),
    )


def _rows(kind: str, rows: object) -> list[dict]:
    # A payload that is not a list is a layout change, not a row defect: it
    # stays a 502. Iterating a dict would yield its keys and skip them all,
    # silently, as an empty 200.
    if not isinstance(rows, list):
        raise UpstreamError()

    valid = [row for row in rows if isinstance(row, dict)]
    for _ in range(len(rows) - len(valid)):
        logger.warning("search %s result skipped, not an object", kind)
    return valid


def _first_row(rows: object) -> object:
    # Not a list stays as is, so _rows still turns it into a 502.
    if isinstance(rows, list):
        return rows[:1]
    return rows


def _complete(kind: str, required: dict[str, object]) -> bool:
    # Required fields are checked by value before the model is built, so a
    # missing one is detected explicitly and nothing is raised, let alone
    # swallowed. Keys are the contract's field names, not the
    # provider's. Neither q nor the row contents are logged.
    missing = [name for name, value in required.items() if value is None]
    if missing:
        logger.warning(
            "search %s result skipped, missing: %s", kind, ", ".join(missing)
        )
        return False
    return True


def _build(
    kind: str, model: type[_ModelT], fields: dict[str, object]
) -> _ModelT | None:
    try:
        return model(**fields)
    except ValidationError as exc:
        # Per-row validation decided by the repo owner in the plan review of
        # #182: one row with a wrong-typed field must not fail the search.
        # It is not the swallow-and-return-empty antipattern of CLAUDE.md
        # because the row is logged (kind and failing field names) and
        # because _require_survivors turns "every row dropped" into a 502.
        # Only the field locations are logged, never the error text (it
        # carries the input value, i.e. the row contents) nor a traceback.
        # Only ValidationError is caught: any other error still reaches
        # translate_upstream_errors().
        failed = ", ".join(
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors(include_input=False, include_url=False)
        )
        logger.warning("search %s result skipped, invalid: %s", kind, failed)
        return None


def _require_survivors(kind: str, rows: list[dict], items: list) -> None:
    # Owner decision (plan review of #182), per list: the provider returned
    # rows and none survived, which signals a format change. A provider
    # answer with no rows is falsy here and stays a normal empty result.
    if rows and not items:
        logger.warning(
            "search %s results all dropped: %d of %d", kind, len(rows), len(rows)
        )
        raise UpstreamError()


def _thumbnails_ok(kind: str, thumbnails: object) -> bool:
    # thumbnails is the provider's container, not a contract field, so it is
    # checked with isinstance (as track_service.py does) to make an odd shape
    # a skipped row and not a KeyError/TypeError that fails the whole search.
    if thumbnails is None or (
        isinstance(thumbnails, list)
        and (not thumbnails or isinstance(thumbnails[-1], dict))
    ):
        return True
    logger.warning("search %s result skipped, invalid: thumbnail_url", kind)
    return False


def _square(url: object) -> object:
    # A non-str reaches the model untouched and fails there as a
    # ValidationError instead of a TypeError inside the regex.
    if isinstance(url, str):
        return square_thumbnail_url(url, _THUMBNAIL_SIZE, smart_crop=True)
    return url


def _map_artist(rows: list[dict]) -> SearchArtist | None:
    if not rows:
        return None

    row = rows[0]
    if not _thumbnails_ok("artist", row.get("thumbnails")):
        return None
    if not _complete("artist", {"id": row.get("browseId"), "name": row.get("artist")}):
        return None

    return _build(
        "artist",
        SearchArtist,
        {
            "id": row.get("browseId"),
            "name": row.get("artist"),
            "thumbnail_url": _square(_thumbnail_url(row.get("thumbnails"))),
        },
    )


def _map_song(row: dict) -> SearchSong | None:
    if not _thumbnails_ok("song", row.get("thumbnails")):
        return None

    # A non-object album is treated as absent (track_service.py does the
    # same): .get on a str would be an AttributeError, a 500.
    album = row.get("album")
    if not isinstance(album, dict):
        album = {}
    fields = {
        "track_id": row.get("videoId"),
        "title": row.get("title"),
        "album": album.get("name"),
        "album_id": album.get("id"),
        "duration_seconds": row.get("duration_seconds"),
        "thumbnail_url": _square(_thumbnail_url(row.get("thumbnails"))),
    }
    if not _complete("song", fields):
        return None

    return _build(
        "song", SearchSong, {**fields, "artists": _artist_refs(row.get("artists"))}
    )


def _map_album(row: dict) -> SearchAlbum | None:
    if not _thumbnails_ok("album", row.get("thumbnails")):
        return None
    if not _complete("album", {"id": row.get("browseId"), "title": row.get("title")}):
        return None

    return _build(
        "album",
        SearchAlbum,
        {
            "id": row.get("browseId"),
            "title": row.get("title"),
            "artists": _artist_refs(row.get("artists")),
            "year": row.get("year"),
            "thumbnail_url": _square(_thumbnail_url(row.get("thumbnails"))),
        },
    )


def _artist_refs(rows: object) -> object:
    # The provider only creates the "artists" key when some part of the
    # subtitle parses as an artist run, so a compilation or a soundtrack
    # row has the key absent, not empty. Started as a copy of
    # services/album_service.py::_artist_refs, which has diverged: that one
    # returns [] for any falsy value and builds SearchArtistRef(**row) itself
    # (a malformed row raises there, a 502), while this one maps only None to
    # [] and hands everything else raw to the model, so a bad shape skips the
    # row. Not a drop-in swap: whoever extracts a shared module must keep each
    # endpoint's behavior.
    #
    # Anything else goes to the model raw, so pydantic validates the whole
    # list and rejects a wrong shape (same idea as the credits names in
    # services/track_service.py).
    if rows is None:
        return []
    return rows


def _thumbnail_url(thumbnails: list[dict] | None) -> str | None:
    # The largest resolution is the last entry. Returning None instead of
    # indexing blindly matters: an empty or absent list would raise
    # IndexError, which core/upstream.py does not translate. The shape was
    # already validated by _thumbnails_ok; a None here skips the song and
    # leaves an album or an artist with a null thumbnail_url.
    if not thumbnails:
        return None

    return thumbnails[-1].get("url")


def _belongs_to_artist(item: _ItemT, artist: SearchArtist) -> bool:
    # a.id is None never matches, even against a None artist.id: an artist
    # mentioned without a link must never be grouped with the primary
    # artist just because both ids happen to be absent.
    return any(a.id is not None and a.id == artist.id for a in item.artists)


def _ordered_by_artist(
    items: list[_ItemT], artist: SearchArtist | None
) -> list[_ItemT]:
    # A stable partition, not a sort: nothing is dropped, and the relative
    # order within each group is the order the provider returned.
    if artist is None:
        return items

    primary = [item for item in items if _belongs_to_artist(item, artist)]
    rest = [item for item in items if not _belongs_to_artist(item, artist)]
    return primary + rest
