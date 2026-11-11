# INFO: Writes the genre playlists of an ingest submission with replace_genre_playlists.

import logging

from supabase import Client

from core.exceptions import NotFound, UpstreamError
from core.upstream import translate_upstream_errors
from models.ingest import GenreIngestRequest, GenreIngestResult

# setup_logging() is called once, in app.py, and is not called again here.
logger = logging.getLogger(__name__)


def ingest_genres(db: Client, request: GenreIngestRequest) -> GenreIngestResult:
    slugs = [genre.slug for genre in request.genres]
    with translate_upstream_errors():
        response = db.table("genres").select("slug").in_("slug", slugs).execute()
        known = {row["slug"] for row in response.data}
    missing = len(set(slugs) - known)
    if missing:
        # The count only: the slugs are the caller's data.
        logger.warning("genre_ingest_unknown_slug count=%d", missing)
        raise NotFound("genre_not_found")

    playlists, tracks = _rpc_params(request)
    expected = sum(len(playlist["tracks"]) for playlist in playlists)
    with translate_upstream_errors():
        response = db.rpc(
            "replace_genre_playlists",
            {"p_playlists": playlists, "p_tracks": tracks},
        ).execute()
        written = response.data

    # bool is an int: True would pass an equality with 1.
    if isinstance(written, bool) or written != expected:
        # The write already happened and a resend leaves the same rows.
        logger.error(
            "genre_ingest_count_mismatch expected=%d returned=%r", expected, written
        )
        raise UpstreamError()

    result = GenreIngestResult(
        genres=len(request.genres), playlists=len(playlists), tracks=expected
    )
    logger.info(
        "genre_ingest genres=%d playlists=%d tracks=%d",
        result.genres,
        result.playlists,
        result.tracks,
    )
    return result


def _rpc_params(request: GenreIngestRequest) -> tuple[list[dict], list[dict]]:
    # sort_order and position come from the order of the lists: the same
    # submission gives the same parameters.
    playlists = [
        {
            "genre_slug": genre.slug,
            "source_playlist_id": playlist.source_playlist_id,
            "title": playlist.title,
            "description": playlist.description,
            "category": playlist.category,
            "sort_order": sort_order,
            "tracks": [
                {"track_id": track_id, "position": position}
                for position, track_id in enumerate(playlist.tracks, start=1)
            ],
        }
        for genre in request.genres
        for sort_order, playlist in enumerate(genre.playlists, start=1)
    ]
    tracks = [track.model_dump() for track in request.tracks]
    return playlists, tracks
