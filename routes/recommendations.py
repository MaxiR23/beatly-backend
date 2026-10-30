# INFO: Personalized recommendations endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id
from core.cache import CacheClient, get_redis
from core.cache_control import private_no_cache
from core.database import get_db
from core.image_color_dependency import ImageColors, image_colors, with_image_colors
from core.search_provider import SearchProvider, get_search_provider
from models.recommendations import Recommendations
from models.responses import ApiSuccess, ok_response
from services.recommendations_service import (
    generate_recommendations,
    get_recommendations,
)

# Router-level on purpose, like the other user-data domains. The database is
# the service-role client (get_db) and not the caller's: user_recommendations
# has no privileges for authenticated (migration 047), so the user_id from
# the token, filtered in every query and RPC of the service, is the only
# barrier.
router = APIRouter(
    prefix="/recommendations",
    tags=["recommendations"],
    dependencies=[Depends(private_no_cache)],
)


@router.get("", response_model=ApiSuccess[Recommendations])
def get_recommendations_route(
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
    colors: ImageColors = Depends(image_colors),  # noqa: B008
) -> ApiSuccess[Recommendations]:
    return with_image_colors(colors, ok_response(get_recommendations(db, user_id)))


@router.post("/generate", response_model=ApiSuccess[Recommendations])
def generate_recommendations_route(
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
    cache: CacheClient = Depends(get_redis),  # noqa: B008
    colors: ImageColors = Depends(image_colors),  # noqa: B008
) -> ApiSuccess[Recommendations]:
    return with_image_colors(
        colors, ok_response(generate_recommendations(db, provider, cache, user_id))
    )
