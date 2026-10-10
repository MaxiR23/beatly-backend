# INFO: Client error report endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id
from core.database import get_db
from models.errors import PlaybackErrorRequest
from models.responses import ApiSuccess, ok_response
from services.error_log_service import record_playback_error

router = APIRouter(prefix="/errors", tags=["errors"])


@router.post("/playback", response_model=ApiSuccess[None])
def report_playback_error_route(
    payload: PlaybackErrorRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[None]:
    record_playback_error(db, user_id, payload)
    return ok_response(None)
