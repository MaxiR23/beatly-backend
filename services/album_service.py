# INFO: Fetches an album and its tracks from the external provider.

import logging

from core.search_provider import (
    SearchProvider,
    provider_get_album,
    provider_get_playlist,
)
from core.upstream import translate_upstream_errors
from models.album import Album, AlbumRef, AlbumTrack
from models.search import SearchArtistRef

# The first module-level logger in a service. It stays here, not in
# core/search_provider.py, because the branch it reports on (an album
# with no audio playlist id gets served with no tracks) is a domain
# decision about albums, not something the four provider domains share.
# setup_logging() is called once, in app.py, and is not called again here.
logger = logging.getLogger(__name__)

# None is the library's "retrieve them all". Its default of 100 is sized
# for user playlists; an album's audio playlist is bounded by the album,
# and a box set silently cut at 100 would be a wrong answer, not a slow
# one. Measured live: 103- and 155-track albums came back whole either
# way, so this costs no extra request today.
_AUDIO_PLAYLIST_LIMIT: int | None = None


def get_album(provider: SearchProvider, album_id: str) -> Album:
    with translate_upstream_errors():
        row = provider_get_album(provider, album_id)
        tracks = _audio_playlist_tracks(provider, album_id, row.get("audioPlaylistId"))

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
            thumbnail_url=_thumbnail_url(row["thumbnails"]),
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


def _audio_playlist_tracks(
    provider: SearchProvider, album_id: str, audio_playlist_id: str | None
) -> list[AlbumTrack]:
    if not audio_playlist_id:
        # Decided by the repo owner: serve the album with no tracks rather
        # than falling back to the album payload's own track list. That
        # payload's ids are music-video ids for a large share of songs
        # (measured: 176 of 389 sampled items), so a fallback would keep
        # /album a source that sometimes hands out video ids and would
        # undo the guarantee this endpoint now makes. Not a 502 either:
        # the provider answered fine. Never observed live (0 of 26
        # albums); the warning is what makes it visible if it ever
        # happens.
        logger.warning("album %s has no audio playlist id", album_id)
        return []

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


def _map_album_ref(row: dict) -> AlbumRef:
    return AlbumRef(
        id=row["browseId"],
        title=row["title"],
        artists=_artist_refs(row.get("artists")),
        year=row.get("year"),
        audio_playlist_id=row.get("audioPlaylistId"),
        thumbnail_url=_thumbnail_url(row["thumbnails"]),
    )


def _artist_refs(rows: list[dict] | None) -> list[SearchArtistRef]:
    # An album with no strapline, and the tracks that inherit it, carry
    # artists: None rather than an empty list. Both normalize to [].
    if not rows:
        return []
    return [SearchArtistRef(**row) for row in rows]


def _thumbnail_url(thumbnails: list[dict] | None) -> str | None:
    # Copied from services/search_service.py, not imported: no service
    # imports another service today. The third consumer is the one that
    # extracts this into a shared module.
    if not thumbnails:
        return None

    return thumbnails[-1]["url"]
