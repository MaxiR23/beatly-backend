# INFO: Likes endpoints.

from datetime import datetime

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id
from core.database import get_db
from models.likes import AddLikeRequest, Like, LikeList
from models.responses import ApiSuccess, ok_response
from services.likes_service import like_track, list_likes, sync_likes, unlike_track

router = APIRouter(prefix="/likes", tags=["likes"])


@router.get("", response_model=ApiSuccess[LikeList])
def get_likes_route(
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[LikeList]:
    return ok_response(list_likes(db, user_id))


@router.get("/sync", response_model=ApiSuccess[LikeList])
def sync_likes_route(
    since: datetime,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[LikeList]:
    return ok_response(sync_likes(db, user_id, since))


@router.post("", response_model=ApiSuccess[Like])
def like_track_route(
    item: AddLikeRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Like]:
    return ok_response(like_track(db, user_id, item))


@router.delete("/{track_id}", response_model=ApiSuccess[None])
def unlike_track_route(
    track_id: str,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[None]:
    unlike_track(db, user_id, track_id)
    return ok_response(None)
