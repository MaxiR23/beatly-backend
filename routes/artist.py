# INFO: Artist endpoint.

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from core.auth import get_current_user_id
from core.cache import CacheClient, get_redis
from core.cache_control import set_max_age
from core.search_provider import SearchProvider, get_search_provider
from models.artist import Artist
from models.responses import ApiSuccess, ok_response
from services.artist_service import get_artist

router = APIRouter(prefix="/artist", tags=["artist"])


@router.get("/{artist_id}", response_model=ApiSuccess[Artist])
def get_artist_route(
    # This pattern is an assumption we make about the provider's id
    # format, not a rule the provider enforces: the library only does
    # channelId.removeprefix("MPLA") and never validates anything. If the
    # provider ever emits a new prefix, the symptom will be a 422 on an
    # artist that actually exists, not a crash.
    artist_id: Annotated[str, Path(pattern=r"^(MPLA)?UC")],
    response: Response,
    # Access policy only: not passed to the service, does not affect the
    # response. This endpoint does not personalize results.
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
    cache: CacheClient = Depends(get_redis),  # noqa: B008
) -> ApiSuccess[Artist]:
    value, remaining = get_artist(provider, cache, artist_id)
    set_max_age(response, remaining)
    return ok_response(value)
