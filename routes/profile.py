# INFO: Profile endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_profile
from core.database import get_db
from models.profiles import Profile, UpdateProfileRequest
from models.responses import ApiSuccess, ok_response
from services.profile_service import update_profile

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("/me", response_model=ApiSuccess[Profile])
def get_my_profile(
    profile: Profile = Depends(get_current_profile),  # noqa: B008
) -> ApiSuccess[Profile]:
    return ok_response(profile)


@router.patch("/me", response_model=ApiSuccess[Profile])
def update_my_profile(
    payload: UpdateProfileRequest,
    profile: Profile = Depends(get_current_profile),  # noqa: B008
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Profile]:
    return ok_response(update_profile(db, profile.id, payload))
