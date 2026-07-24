# INFO: Genres endpoints.

from fastapi import APIRouter

from models.genres import GenreList
from models.responses import ApiSuccess, ok_response
from services.genre_service import list_genres

router = APIRouter(prefix="/genres", tags=["genres"])


@router.get("", response_model=ApiSuccess[GenreList])
def get_genres() -> ApiSuccess[GenreList]:
    return ok_response(list_genres())
