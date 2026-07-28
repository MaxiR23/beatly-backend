# INFO: Playlist endpoints.

from uuid import UUID

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id
from core.database import get_db
from models.playlists import (
    AddPlaylistTrackRequest,
    BulkAddPlaylistTracksRequest,
    BulkAddResult,
    CreatePlaylistRequest,
    MovePlaylistTrackRequest,
    OwnedPlaylistIds,
    Playlist,
    PlaylistDetail,
    PlaylistList,
    PlaylistTrack,
    UpdatePlaylistRequest,
)
from models.responses import ApiSuccess, ok_response
from services.playlist_service import (
    add_track,
    add_tracks,
    create_playlist,
    delete_playlist,
    get_playlist,
    list_owned_playlists_with_track,
    list_playlists,
    move_track,
    remove_track,
    update_playlist,
)

router = APIRouter(prefix="/playlists", tags=["playlists"])


@router.post("", response_model=ApiSuccess[Playlist])
def create_playlist_route(
    item: CreatePlaylistRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Playlist]:
    return ok_response(create_playlist(db, user_id, item))


@router.get("", response_model=ApiSuccess[PlaylistList])
def list_playlists_route(
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[PlaylistList]:
    return ok_response(list_playlists(db, user_id))


# Declared before /{playlist_id} so the literal path is matched first. Its
# track_id is the provider id, not a uuid: it is the id a client holds while
# playing a track.
@router.get("/owned-with-track/{track_id}", response_model=ApiSuccess[OwnedPlaylistIds])
def list_owned_playlists_with_track_route(
    track_id: str,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[OwnedPlaylistIds]:
    return ok_response(list_owned_playlists_with_track(db, user_id, track_id))


@router.get("/{playlist_id}", response_model=ApiSuccess[PlaylistDetail])
def get_playlist_route(
    playlist_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[PlaylistDetail]:
    return ok_response(get_playlist(db, user_id, str(playlist_id)))


@router.patch("/{playlist_id}", response_model=ApiSuccess[Playlist])
def update_playlist_route(
    playlist_id: UUID,
    item: UpdatePlaylistRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Playlist]:
    return ok_response(update_playlist(db, user_id, str(playlist_id), item))


@router.delete("/{playlist_id}", response_model=ApiSuccess[None])
def delete_playlist_route(
    playlist_id: UUID,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[None]:
    delete_playlist(db, user_id, str(playlist_id))
    return ok_response(None)


# Declared before /{playlist_id}/tracks so the literal path is matched first.
@router.post("/{playlist_id}/tracks/bulk", response_model=ApiSuccess[BulkAddResult])
def add_playlist_tracks_route(
    playlist_id: UUID,
    item: BulkAddPlaylistTracksRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[BulkAddResult]:
    return ok_response(add_tracks(db, user_id, str(playlist_id), item))


@router.post("/{playlist_id}/tracks", response_model=ApiSuccess[PlaylistTrack])
def add_playlist_track_route(
    playlist_id: UUID,
    item: AddPlaylistTrackRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[PlaylistTrack]:
    return ok_response(add_track(db, user_id, str(playlist_id), item))


@router.delete("/{playlist_id}/tracks/{track_id}", response_model=ApiSuccess[None])
def remove_playlist_track_route(
    playlist_id: UUID,
    track_id: str,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[None]:
    remove_track(db, user_id, str(playlist_id), track_id)
    return ok_response(None)


@router.post("/{playlist_id}/move-track", response_model=ApiSuccess[None])
def move_playlist_track_route(
    playlist_id: UUID,
    item: MovePlaylistTrackRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[None]:
    move_track(db, user_id, str(playlist_id), item)
    return ok_response(None)
