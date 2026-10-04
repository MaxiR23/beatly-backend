# INFO: Fetches an album and its tracks from the external provider.

import logging

from core.cache import CacheClient, cache_get, cache_key, cache_set, cache_ttl
from core.search_provider import (
    SearchProvider,
    provider_get_album,
    provider_get_playlist,
)
from core.thumbnails import square_thumbnail_url
from core.upstream import translate_upstream_errors
from models.album import Album, AlbumRef, AlbumTrack
from models.search import SearchArtistRef

# The first module-level logger in a service. It stays here, not in
# core/search_provider.py, because the branch it reports on (an album
# with no audio playlist id gets its tracks from its own list) is a domain
# decision about albums, not something the four provider domains share.
# setup_logging() is called once, in app.py, and is not called again here.
logger = logging.getLogger(__name__)

# None is the library's "retrieve them all". Its default of 100 is sized
# for user playlists; an album's audio playlist is bounded by the album,
# and a box set silently cut at 100 would be a wrong answer, not a slow
# one. Measured live: 103- and 155-track albums came back whole either
# way, so this costs no extra request today.
_AUDIO_PLAYLIST_LIMIT: int | None = None

# TTL for a cached album: the provider's own catalog data, stable enough
# to serve up to a day old.
_ALBUM_TTL_SECONDS = 24 * 60 * 60

# Side of the square image requested from the provider's CDN, with smart
# crop, for the album cover and its references. The rule is the one in
# core/thumbnails.py, so a larger URL is rewritten down too.
_THUMBNAIL_SIZE = 544


def get_album(
    provider: SearchProvider, cache: CacheClient, album_id: str
) -> tuple[Album, int | None]:
    key = cache_key("album", album_id)
    # The read stays outside translate_upstream_errors(): a ValidationError
    # from a stale cached value is a cache failure, not a 502, and
    # core/upstream.py must never see it.
    cached = cache_get(cache, key, Album)
    if cached is not None:
        return cached, cache_ttl(cache, key)

    album = _fetch_album(provider, album_id)
    cache_set(cache, key, album, ttl=_ALBUM_TTL_SECONDS)
    return album, _ALBUM_TTL_SECONDS


def _fetch_album(provider: SearchProvider, album_id: str) -> Album:
    with translate_upstream_errors():
        row = provider_get_album(provider, album_id)
        tracks = _album_tracks(
            provider,
            album_id,
            row.get("audioPlaylistId"),
            row.get("tracks") or [],
        )

        return Album(
            id=album_id,  # the provider's response never carries the id back
            title=row["title"],
            # year: absent when the provider's header has no subtitle runs.
            year=row.get("year"),
            artists=_artist_refs(row.get("artists")),
            # trackCount: absent in the branch of the provider's header
            # parser that only sets duration, not a broken layout.
            track_count=row.get("trackCount"),
            duration_seconds=row["duration_seconds"],
            audio_playlist_id=row.get("audioPlaylistId"),
            thumbnail_url=square_thumbnail_url(
                _thumbnail_url(row["thumbnails"]), _THUMBNAIL_SIZE, smart_crop=True
            ),
            tracks=tracks,
            # other_versions/related_recommendations: absent when the
            # provider's page has no such carousel, not an empty list.
            other_versions=[
                _map_album_ref(ref) for ref in row.get("other_versions", [])
            ],
            related_recommendations=[
                _map_album_ref(ref) for ref in row.get("related_recommendations", [])
            ],
        )


def _album_tracks(
    provider: SearchProvider,
    album_id: str,
    audio_playlist_id: str | None,
    album_tracks: list[dict],
) -> list[AlbumTrack]:
    if not audio_playlist_id:
        # Decided by the repo owner: with no audio playlist id the tracks
        # come from the album's own list, with the same rule as the
        # unavailable branch below (available ones carry the payload's
        # videoId, unavailable ones a null track_id). The payload's ids
        # can be music-video ids, which is the accepted cost of having
        # tracks at all here. An album whose payload has no tracks either
        # gets an empty list: an expected empty state, not a 502, because
        # the provider answered fine. Never observed live (0 of 26
        # albums); the warning is what makes it visible if it happens.
        logger.warning(
            "album %s has no audio playlist id, served from its own track list",
            album_id,
        )
        return [_map_album_payload_track(t, i) for i, t in enumerate(album_tracks)]

    # Decided by the repo owner: the source is chosen from the album's own
    # response, with no second request. With any track unavailable the
    # audio playlist can arrive without "contents" (measured with DAMN.,
    # 14 tracks, 9 unavailable) and the library breaks on it with a
    # KeyError, so the tracks come from the album's own list and the
    # playlist is not requested. isAvailable is indexed: the library sets
    # it on every item, so its absence is a layout change and has to be a
    # 502. An album with no tracks in its payload does not get here
    # (all([]) is true): it follows the audio playlist, and if that fails
    # it is a 502, never a silent empty list.
    if not all(track["isAvailable"] for track in album_tracks):
        logger.warning(
            "album %s served from its own track list: some tracks are unavailable",
            album_id,
        )
        return [_map_album_payload_track(t, i) for i, t in enumerate(album_tracks)]

    playlist = provider_get_playlist(
        provider, audio_playlist_id, limit=_AUDIO_PLAYLIST_LIMIT
    )
    # Indexed, not .get(..., []): the library always sets this key on a
    # parsed playlist, so its absence is a layout change and has to be a
    # 502, never an empty track list.
    return [_map_track(track, i) for i, track in enumerate(playlist["tracks"])]


def _map_track(row: dict, index: int) -> AlbumTrack:
    # The library only sets trackNumber when it parses an item with
    # is_album=True, and the audio playlist these tracks come from never
    # goes through that branch, so the provider's own trackNumber is
    # never present here, not just for unavailable tracks. The position
    # is the only source for track_number, and it produces the same
    # values the provider's own trackNumber did (measured: 0
    # discrepancies in 589 tracks), keeping the album's numbering
    # contiguous.
    track_number = index + 1

    return AlbumTrack(
        track_id=row.get("videoId"),
        title=row["title"],
        artists=_artist_refs(row.get("artists")),
        duration_seconds=row.get("duration_seconds"),
        is_available=row["isAvailable"],
        track_number=track_number,
    )


def _map_album_payload_track(row: dict, index: int) -> AlbumTrack:
    is_available = row["isAvailable"]

    # The position, not the provider's trackNumber: it is None for the
    # unavailable tracks, and the position keeps the numbering contiguous.
    # track_id is the payload's videoId as is, which can be a music-video
    # id (measured: the 5 available tracks of DAMN. are all video ids with
    # no audio counterpart), so it may not work with /credits. It is null
    # for an unavailable track. duration_seconds keeps the album's real
    # duration, also for unavailable tracks (repo owner decision); .get
    # because the library only sets the key when there is a duration.
    # Artists may be the album's own: the library copies them into a track
    # that has none.
    return AlbumTrack(
        track_id=row.get("videoId") if is_available else None,
        title=row["title"],
        artists=_artist_refs(row.get("artists")),
        duration_seconds=row.get("duration_seconds"),
        is_available=is_available,
        track_number=index + 1,
    )


def _map_album_ref(row: dict) -> AlbumRef:
    return AlbumRef(
        id=row["browseId"],
        title=row["title"],
        artists=_artist_refs(row.get("artists")),
        year=row.get("year"),
        audio_playlist_id=row.get("audioPlaylistId"),
        thumbnail_url=square_thumbnail_url(
            _thumbnail_url(row["thumbnails"]), _THUMBNAIL_SIZE, smart_crop=True
        ),
    )


def _artist_refs(rows: list[dict] | None) -> list[SearchArtistRef]:
    # An album with no strapline, and the tracks that inherit it, carry
    # artists: None rather than an empty list. Both normalize to [].
    if not rows:
        return []
    return [SearchArtistRef(**row) for row in rows]


def _thumbnail_url(thumbnails: list[dict] | None) -> str | None:
    # Started as a copy of services/search_service.py::_thumbnail_url, which
    # has diverged: that one reads the url with .get (a missing "url" gives
    # None, because the row shape was already validated and a bad row is
    # skipped), this one indexes ["url"] (a missing key raises, a 502).
    # Not a drop-in swap: whoever extracts a shared module must keep each
    # endpoint's behavior.
    if not thumbnails:
        return None

    return thumbnails[-1]["url"]
