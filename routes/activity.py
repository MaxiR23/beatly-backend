# INFO: Play event and recent activity endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id
from core.database import get_db
from models.activity import (
    LogPlayRequest,
    PlayEvent,
    RecentEntity,
    RecentEntityList,
    RegisterRecentRequest,
)
from models.responses import ApiSuccess, ok_response
from services.activity_service import list_recents, log_play, register_recent

plays_router = APIRouter(prefix="/plays", tags=["plays"])
recents_router = APIRouter(prefix="/recents", tags=["recents"])


@plays_router.post("", response_model=ApiSuccess[PlayEvent])
def log_play_route(
    item: LogPlayRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[PlayEvent]:
    return ok_response(log_play(db, user_id, item))


@recents_router.get("", response_model=ApiSuccess[RecentEntityList])
def get_recents_route(
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[RecentEntityList]:
    return ok_response(list_recents(db, user_id))


@recents_router.post("", response_model=ApiSuccess[RecentEntity])
def register_recent_route(
    item: RegisterRecentRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[RecentEntity]:
    return ok_response(register_recent(db, user_id, item))
