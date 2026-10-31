# INFO: Maps an album row of the provider to an AlbumRef, shared by every service that lists albums.

from core.thumbnails import square_thumbnail_url
from models.album import AlbumRef

# Side of the square image, with smart crop, that the three copies of this
# mapping asked the provider's CDN for. The rule is the one in
# core/thumbnails.py.
_THUMBNAIL_SIZE = 544


def map_album_ref(row: dict, *, fallback_artist: dict | None = None) -> AlbumRef:
    # Replaces three identical copies (artist, album and track services,
    # #229). The other mapping rules stay private to each service because
    # their copies carry domain reasons of their own or have diverged
    # (search).
    #
    # The artists and thumbnail rules are the ones of those three services,
    # not the ones of services/search_service.py: a thumbnail without "url"
    # is a KeyError (a 502 in the caller), not None. Changing this function
    # changes /artist, /public/artist, /album and /tracks/{id}/related at
    # once.
    #
    # fallback_artist is only used when the row carries no artist at all.
    # A row with artists keeps them as they are, even when the fallback is
    # not among them. Only a caller that knows whose album it is passes it.
    #
    # No try/except: a missing key is translated by the
    # translate_upstream_errors() of each caller.
    thumbnails = row["thumbnails"]
    return AlbumRef(
        id=row["browseId"],
        title=row["title"],
        # The rows go to the model raw: it builds the artist references.
        artists=row.get("artists")
        or ([fallback_artist] if fallback_artist is not None else []),
        year=row.get("year"),
        audio_playlist_id=row.get("audioPlaylistId"),
        thumbnail_url=square_thumbnail_url(
            thumbnails[-1]["url"] if thumbnails else None,
            _THUMBNAIL_SIZE,
            smart_crop=True,
        ),
    )
