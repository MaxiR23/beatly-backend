# INFO: Fetches an album and its tracks from the external provider.

from core.search_provider import SearchProvider, provider_get_album
from core.upstream import translate_upstream_errors
from models.album import Album, AlbumRef, AlbumTrack
from models.search import SearchArtistRef


def get_album(provider: SearchProvider, album_id: str) -> Album:
    with translate_upstream_errors():
        row = provider_get_album(provider, album_id)

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
            tracks=[_map_track(track, i) for i, track in enumerate(row["tracks"])],
            # other_versions/related_recommendations: absent when the
            # provider's page has no such carousel, not an empty list.
            other_versions=[
                _map_album_ref(ref) for ref in row.get("other_versions", [])
            ],
            related_recommendations=[
                _map_album_ref(ref) for ref in row.get("related_recommendations", [])
            ],
        )


def _map_track(row: dict, index: int) -> AlbumTrack:
    track_number = row.get("trackNumber")
    if track_number is None:
        # Unavailable tracks carry trackNumber: None. Falling back to the
        # track's position keeps the album's numbering contiguous.
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
