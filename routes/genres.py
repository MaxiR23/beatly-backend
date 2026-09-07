# INFO: Genres endpoints.

from uuid import UUID

from fastapi import APIRouter, Depends
from supabase import Client

from core.database import get_db
from models.genres import Genre, GenrePlaylist, GenrePlaylistTrack
from models.responses import ApiSuccess, Paginated, ok_response
from services.genre_service import (
    get_genre_categories,
    get_genre_playlist_tracks,
    get_genre_playlists,
    list_genres,
)

router = APIRouter(prefix="/genres", tags=["genres"])
genre_playlists_router = APIRouter(prefix="/genre-playlists", tags=["genres"])


@router.get("", response_model=ApiSuccess[Paginated[Genre]])
def get_genres(
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Paginated[Genre]]:
    items, page_block = list_genres(db)
    return ok_response(Paginated(items=items, page=page_block))


@router.get("/{slug}/playlists", response_model=ApiSuccess[Paginated[GenrePlaylist]])
def get_genre_playlists_route(
    slug: str,
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Paginated[GenrePlaylist]]:
    items, page_block = get_genre_playlists(db, slug)
    return ok_response(Paginated(items=items, page=page_block))


@router.get("/{slug}/categories", response_model=ApiSuccess[Paginated[str]])
def get_genre_categories_route(
    slug: str,
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Paginated[str]]:
    items, page_block = get_genre_categories(db, slug)
    return ok_response(Paginated(items=items, page=page_block))


@genre_playlists_router.get(
    "/{playlist_id}/tracks", response_model=ApiSuccess[Paginated[GenrePlaylistTrack]]
)
def get_genre_playlist_tracks_route(
    playlist_id: UUID,
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Paginated[GenrePlaylistTrack]]:
    items, page_block = get_genre_playlist_tracks(db, str(playlist_id))
    return ok_response(Paginated(items=items, page=page_block))
