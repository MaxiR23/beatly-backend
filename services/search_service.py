# INFO: Searches the external provider for artists, songs and albums.

from typing import TypeVar

from core.search_provider import SearchProvider, provider_search
from core.upstream import translate_upstream_errors
from models.search import (
    SearchAlbum,
    SearchArtist,
    SearchArtistRef,
    SearchResult,
    SearchSong,
)

_ItemT = TypeVar("_ItemT", SearchSong, SearchAlbum)


def search(provider: SearchProvider, q: str) -> SearchResult:
    with translate_upstream_errors():
        artist_rows = provider_search(provider, q, filter="artists", limit=1)
        song_rows = provider_search(provider, q, filter="songs")
        album_rows = provider_search(provider, q, filter="albums")

        artist = _map_artist(artist_rows)
        songs = [_map_song(row) for row in song_rows]
        albums = [_map_album(row) for row in album_rows]

    # Pure list reshuffling: no provider call and no model construction can
    # fail here, so it stays outside the translated block.
    return SearchResult(
        artist=artist,
        songs=_ordered_by_artist(songs, artist),
        albums=_ordered_by_artist(albums, artist),
    )


def _map_artist(rows: list[dict]) -> SearchArtist | None:
    if not rows:
        return None

    row = rows[0]
    return SearchArtist(id=row["browseId"], name=row["artist"])


def _map_song(row: dict) -> SearchSong:
    return SearchSong(
        track_id=row["videoId"],
        title=row["title"],
        artists=[SearchArtistRef(**artist) for artist in row["artists"]],
        album=row["album"]["name"],
        album_id=row["album"]["id"],
        duration_seconds=row["duration_seconds"],
        thumbnail_url=_thumbnail_url(row["thumbnails"]),
    )


def _map_album(row: dict) -> SearchAlbum:
    return SearchAlbum(
        id=row["browseId"],
        playlist_id=row["playlistId"],
        title=row["title"],
        artists=[SearchArtistRef(**artist) for artist in row["artists"]],
        year=row["year"],
        thumbnail_url=_thumbnail_url(row["thumbnails"]),
    )


def _thumbnail_url(thumbnails: list[dict] | None) -> str | None:
    # The largest resolution is the last entry. Returning None instead of
    # indexing blindly matters: an empty or absent list would raise
    # IndexError, which core/upstream.py does not translate, while None
    # here becomes either a 502 (SearchSong.thumbnail_url is required) or a
    # legitimate absence (SearchAlbum.thumbnail_url is nullable).
    if not thumbnails:
        return None

    return thumbnails[-1]["url"]


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
