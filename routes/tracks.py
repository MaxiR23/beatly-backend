# INFO: Track endpoints: a track's up-next queue, lyrics, related content and credits.

from fastapi import APIRouter, Depends

from core.auth import get_current_user_id
from core.search_provider import SearchProvider, get_search_provider
from models.responses import ApiSuccess, ok_response
from models.track import TrackCredits, TrackLyricsResult, TrackRelated, TrackUpNext
from services.track_service import (
    get_track_credits,
    get_track_lyrics,
    get_track_related,
    get_track_upnext,
)

router = APIRouter(prefix="/tracks", tags=["tracks"])


# Unlike routes/album.py and routes/artist.py, track_id takes no
# Path(pattern=...): those two need the pattern because, without it, a
# malformed id and a nonexistent one collapse into the same 502, and here
# that ambiguity is already broken by D1's structured 404 (measured: "abc"
# comes back with playabilityStatus.status == "ERROR", i.e. the correct 404
# track_not_found). A pattern here would be our own assumption about the
# provider's id format with nothing to buy in exchange, and its only
# possible symptom would be a 422 on a track that actually exists.


@router.get("/{track_id}/upnext", response_model=ApiSuccess[TrackUpNext])
def get_track_upnext_route(
    track_id: str,
    # Access policy only: not passed to the service, does not affect the
    # response. This endpoint does not personalize results.
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[TrackUpNext]:
    return ok_response(get_track_upnext(provider, track_id))


@router.get("/{track_id}/lyrics", response_model=ApiSuccess[TrackLyricsResult])
def get_track_lyrics_route(
    track_id: str,
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[TrackLyricsResult]:
    return ok_response(get_track_lyrics(provider, track_id))


@router.get("/{track_id}/related", response_model=ApiSuccess[TrackRelated])
def get_track_related_route(
    track_id: str,
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[TrackRelated]:
    return ok_response(get_track_related(provider, track_id))


@router.get("/{track_id}/credits", response_model=ApiSuccess[TrackCredits])
def get_track_credits_route(
    track_id: str,
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[TrackCredits]:
    return ok_response(get_track_credits(provider, track_id))
