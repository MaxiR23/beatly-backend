# INFO: Album endpoint.

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from core.auth import get_current_user_id
from core.search_provider import SearchProvider, get_search_provider
from models.album import Album
from models.responses import ApiSuccess, ok_response
from services.album_service import get_album

router = APIRouter(prefix="/album", tags=["album"])


@router.get("/{album_id}", response_model=ApiSuccess[Album])
def get_album_route(
    # The external provider requires this prefix; declaring it here rejects
    # a malformed id with 422 before spending an outgoing call. It also
    # keeps the provider's own usage-error class unreachable: loosening
    # this pattern without also mapping that class would surface as a 500.
    album_id: Annotated[str, Path(pattern=r"^MPRE")],
    # Access policy only: not passed to the service, does not affect the
    # response. This endpoint does not personalize results.
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[Album]:
    return ok_response(get_album(provider, album_id))
