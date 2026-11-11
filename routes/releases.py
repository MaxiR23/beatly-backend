# INFO: New releases endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.database import get_db
from core.image_color_dependency import ImageColors, image_colors, with_image_colors
from core.pagination import PageRequest, page_params
from models.releases import NewRelease
from models.responses import ApiSuccess, Paginated, ok_response
from services.release_service import list_new_releases

# Public like /genres: no token, and the Cache-Control comes from the app's
# default (no-store). The read uses the service-role client (get_db) because
# the table has no privileges for the client roles (migration 053).
router = APIRouter(prefix="/releases", tags=["releases"])


@router.get("", response_model=ApiSuccess[Paginated[NewRelease]])
def get_releases(
    page: PageRequest = Depends(page_params),  # noqa: B008
    db: Client = Depends(get_db),  # noqa: B008
    colors: ImageColors = Depends(image_colors),  # noqa: B008
) -> ApiSuccess[Paginated[NewRelease]]:
    items, page_block = list_new_releases(db, page)
    return with_image_colors(
        colors, ok_response(Paginated(items=items, page=page_block))
    )
