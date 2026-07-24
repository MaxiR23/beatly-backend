# INFO: Genres endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.database import get_db
from models.genres import GenreList
from models.responses import ApiSuccess, ok_response
from services.genre_service import list_genres

router = APIRouter(prefix="/genres", tags=["genres"])


@router.get("", response_model=ApiSuccess[GenreList])
def get_genres(db: Client = Depends(get_db)) -> ApiSuccess[GenreList]:  # noqa: B008
    return ok_response(list_genres(db))
