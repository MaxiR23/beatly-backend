# INFO: Composes the public share payloads from the existing domain services.

# The one service in this repo that imports other services (0 of 11 did
# before this file). Every other router imports from exactly one service
# module; a public share endpoint is cross-domain by definition, so one of
# the two conventions had to give. This module is the deliberate
# exception: the entire public contract (which fields are dropped, how
# `owner` is built, what gets summed in Python) lives in one reviewable
# place instead of leaking into album_service.py, artist_service.py,
# playlist_service.py and genre_service.py one field at a time. It does
# no I/O of its own: no db.table(...) call and no provider call live here,
# only calls into the services that already own that logic.

from supabase import Client

from core.search_provider import SearchProvider
from models.public import (
    PublicAlbum,
    PublicArtist,
    PublicGenrePlaylist,
    PublicOwner,
    PublicPlaylist,
    PublicPlaylistTrack,
    PublicTrack,
)
from services.album_service import get_album
from services.artist_service import get_artist
from services.genre_service import (
    get_genre_playlist,
    get_genre_playlist_thumbnails,
    get_genre_playlist_tracks,
)
from services.playlist_service import get_public_playlist, get_user_playlist_thumbnails
from services.track_service import get_track


def get_public_album(provider: SearchProvider, album_id: str) -> PublicAlbum:
    album = get_album(provider, album_id)
    # exclude, not a whitelist: the diff shows exactly what a public share
    # card drops (the internal audio playlist id and the two browse
    # carousels), rather than hiding it behind a field-by-field copy.
    return PublicAlbum(
        **album.model_dump(
            exclude={"audio_playlist_id", "other_versions", "related_recommendations"}
        )
    )


def get_public_artist(provider: SearchProvider, artist_id: str) -> PublicArtist:
    artist = get_artist(provider, artist_id)
    return PublicArtist(**artist.model_dump(exclude={"related"}))


def get_public_track(provider: SearchProvider, track_id: str) -> PublicTrack:
    track = get_track(provider, track_id)
    # No exclude, unlike album and artist above: PublicTrack has no field
    # that TrackRef does not, so nothing is dropped.
    return PublicTrack(**track.model_dump())


def get_public_user_playlist(db: Client, playlist_id: str) -> PublicPlaylist:
    detail = get_public_playlist(db, playlist_id)
    thumbnails = get_user_playlist_thumbnails(db, playlist_id)

    # Mapped field by field, not model_dump()'d wholesale: owner_id,
    # is_public, created_at and updated_at are deliberately not copied,
    # which is what keeps this response silent about who owns the
    # playlist -- profiles are not readable by third parties.
    return PublicPlaylist(
        id=detail.id,
        title=detail.title,
        description=detail.description,
        owner=PublicOwner(type="user"),
        track_count=detail.total_count,
        has_more=detail.has_more,
        total_duration_seconds=detail.total_duration_seconds,
        tracks=[
            PublicPlaylistTrack(**track.model_dump(exclude={"id"}))
            for track in detail.tracks
        ],
        thumbnails=thumbnails,
    )


def get_public_genre_playlist(db: Client, playlist_id: str) -> PublicGenrePlaylist:
    playlist = get_genre_playlist(db, playlist_id)
    tracks, block = get_genre_playlist_tracks(db, playlist_id)
    thumbnails = get_genre_playlist_thumbnails(db, playlist_id)

    return PublicGenrePlaylist(
        id=playlist.id,
        title=playlist.title,
        description=playlist.description,
        owner=PublicOwner(type="app"),
        # The number of tracks actually returned, not the stored
        # genre_playlists.track_count column: this DTO carries the list
        # itself, so a stored counter that disagreed with it would be an
        # inconsistency visible in the same response.
        track_count=len(tracks),
        has_more=block.has_more,
        # Summed in Python, not by an RPC (R5): get_genre_playlist_tracks()
        # reads the collection whole, via _whole_collection(rows), with no
        # cap like the _TRACKS_LIMIT that applies to a user playlist -- so
        # this sum is exact, unlike the reason #112 avoided doing the same
        # for likes.
        total_duration_seconds=sum(track.duration_seconds for track in tracks),
        tracks=[PublicPlaylistTrack(**track.model_dump()) for track in tracks],
        thumbnails=thumbnails,
        thumbnail_url=playlist.thumbnail_url,
    )
