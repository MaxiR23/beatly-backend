# INFO: Fetches an artist's page (top songs, albums, singles and related artists) from the external provider.

from core.search_provider import SearchProvider, provider_get_artist
from core.upstream import translate_upstream_errors
from models.album import AlbumRef
from models.artist import Artist, ArtistRef, ArtistRelease, ArtistSong
from models.search import SearchArtistRef


def get_artist(provider: SearchProvider, artist_id: str) -> Artist:
    with translate_upstream_errors():
        row = provider_get_artist(provider, artist_id)

        return Artist(
            id=artist_id,  # the provider's channelId is a different (video)
            # channel, not the id that was requested; never read it back.
            name=row["name"],
            thumbnail_url=_thumbnail_url(row["thumbnails"]),
            songs=[_map_song(s) for s in _song_rows(row)],
            albums=[_map_album_ref(a) for a in _section_rows(row, "albums")],
            singles=[_map_release(s) for s in _section_rows(row, "singles")],
            related=[_map_related(r) for r in _section_rows(row, "related")],
        )


def _section_rows(row: dict, key: str) -> list[dict]:
    # A category the artist has no section for is absent from the response
    # entirely rather than empty: parse_channel_contents only creates it
    # `if len(data) > 0`. That branch is documented, so it returns [].
    #
    # Past that the section is indexed, never .get()-chained: a section
    # that stopped being a dict raises TypeError, and one without
    # "results" raises KeyError, and core/upstream.py translates both to
    # 502. A .get() chain would raise AttributeError, which it does not
    # translate and which would surface as a 500, and an inner default
    # would hide the same layout change behind a silently empty list --
    # the one shape docs/api/artists.md promises never to return.
    return row.get(key, {"results": []})["results"]


def _song_rows(row: dict) -> list[dict]:
    # songs is the one section whose "results" key is genuinely optional:
    # the provider always creates the category dict but only fills it when
    # the page carries that shelf ("API sometimes does not return songs").
    # Merging over the default covers that branch and the category being
    # absent, while a section that stopped being a dict still raises
    # TypeError -> 502, which a .get() chain would not.
    return ({"results": []} | row.get("songs", {}))["results"]


def _map_song(row: dict) -> ArtistSong:
    album, album_id = _song_album(row.get("album"))
    return ArtistSong(
        track_id=row.get("videoId"),
        title=row["title"],
        artists=_artist_refs(row.get("artists")),
        album=album,
        album_id=album_id,
        # duration_seconds: the key is only added `if duration:`.
        duration_seconds=row.get("duration_seconds"),
        thumbnail_url=_thumbnail_url(row["thumbnails"]),
    )


def _song_album(album: dict | None) -> tuple[str | None, str | None]:
    # album/album_id always travel together. The provider can send a name
    # with no id ({"name": <text>, "id": None}); losing the name in that
    # case is deliberate, so the pair is always coherent.
    if album is None or album["id"] is None:
        return None, None
    return album["name"], album["id"]


def _map_album_ref(row: dict) -> AlbumRef:
    return AlbumRef(
        id=row["browseId"],
        title=row["title"],
        artists=_artist_refs(row.get("artists")),
        year=row.get("year"),
        audio_playlist_id=row.get("audioPlaylistId"),
        thumbnail_url=_thumbnail_url(row["thumbnails"]),
    )


def _map_release(row: dict) -> ArtistRelease:
    return ArtistRelease(
        id=row["browseId"],
        title=row["title"],
        # year/type: both conditional keys from
        # _parse_album_single_subtitle, absent when not applicable.
        year=row.get("year"),
        type=row.get("type"),
        thumbnail_url=_thumbnail_url(row["thumbnails"]),
    )


def _map_related(row: dict) -> ArtistRef:
    # subscribers is deliberately not read: it is not exposed by this
    # endpoint.
    return ArtistRef(
        id=row["browseId"],
        name=row["title"],
        thumbnail_url=_thumbnail_url(row["thumbnails"]),
    )


def _artist_refs(rows: list[dict] | None) -> list[SearchArtistRef]:
    # The provider either omits the "artists" key or sets it to None when
    # nothing in the row parses as an artist run: parse_playlist_item
    # builds a song with artists=None when no flex column resolves to one.
    # Both normalize to [].
    #
    # Copied, not imported: no service imports another service today. The
    # third consumer is the one that extracts this into a shared module,
    # and this change is that third consumer -- the extraction is left out
    # of it deliberately, not renumbered: doing it here would rewrite
    # services/album_service.py and services/search_service.py, whose
    # copies carry domain reasons this one does not, inside a change that
    # adds an endpoint. The rule stands as written; the extraction is its
    # own change.
    if not rows:
        return []
    return [SearchArtistRef(**row) for row in rows]


def _thumbnail_url(thumbnails: list[dict] | None) -> str | None:
    # The largest resolution is the last entry. Returning None instead of
    # indexing blindly matters: an empty or absent list would raise
    # IndexError, which core/upstream.py does not translate, so it would
    # surface as a 500 instead of the 502 every other provider failure
    # gets.
    #
    # Copied, not imported, for the same reason as _artist_refs above.
    if not thumbnails:
        return None

    return thumbnails[-1]["url"]
