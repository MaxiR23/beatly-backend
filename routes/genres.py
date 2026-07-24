# INFO: Genres endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.database import get_db
from models.genres import GenreCategoryList, GenreList, GenrePlaylistList
from models.responses import ApiSuccess, ok_response
from services.genre_service import (
    get_genre_categories,
    get_genre_playlists,
    list_genres,
)

router = APIRouter(prefix="/genres", tags=["genres"])


@router.get("", response_model=ApiSuccess[GenreList])
def get_genres(db: Client = Depends(get_db)) -> ApiSuccess[GenreList]:  # noqa: B008
    return ok_response(list_genres(db))


@router.get("/{slug}/playlists", response_model=ApiSuccess[GenrePlaylistList])
def get_genre_playlists_route(
    slug: str,
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[GenrePlaylistList]:
    return ok_response(get_genre_playlists(db, slug))


@router.get("/{slug}/categories", response_model=ApiSuccess[GenreCategoryList])
def get_genre_categories_route(
    slug: str,
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[GenreCategoryList]:
    return ok_response(get_genre_categories(db, slug))
