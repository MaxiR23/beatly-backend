# INFO: Search endpoint.

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from core.auth import get_current_user_id
from core.search_provider import SearchProvider, get_search_provider
from models.responses import ApiSuccess, ok_response
from models.search import SearchResult
from services.search_service import search

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=ApiSuccess[SearchResult])
def search_route(
    q: Annotated[str, Query(min_length=1)],
    # Access policy only: not passed to the service, does not affect the
    # response. This endpoint does not personalize results.
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[SearchResult]:
    return ok_response(search(provider, q))
