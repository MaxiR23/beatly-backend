# INFO: Play event and recent activity endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id
from core.database import get_db
from core.pagination import PageRequest, page_params
from models.activity import (
    LogPlayRequest,
    PlayEvent,
    RecentEntity,
    RegisterRecentRequest,
)
from models.responses import ApiSuccess, Paginated, ok_response
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


@recents_router.get("", response_model=ApiSuccess[Paginated[RecentEntity]])
def get_recents_route(
    page: PageRequest = Depends(page_params),  # noqa: B008
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Paginated[RecentEntity]]:
    items, page_block = list_recents(db, user_id, page)
    return ok_response(Paginated(items=items, page=page_block))


@recents_router.post("", response_model=ApiSuccess[RecentEntity])
def register_recent_route(
    item: RegisterRecentRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[RecentEntity]:
    return ok_response(register_recent(db, user_id, item))
