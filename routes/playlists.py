# INFO: Playlist endpoints.

from uuid import UUID

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id
from core.database import get_db
from models.playlists import (
    CreatePlaylistRequest,
    Playlist,
    PlaylistDetail,
    PlaylistList,
    UpdatePlaylistRequest,
)
from models.responses import ApiSuccess, ok_response
from services.playlist_service import (
    create_playlist,
    delete_playlist,
    get_playlist,
    list_playlists,
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
