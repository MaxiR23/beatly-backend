# INFO: Track endpoints: a track's up-next queue, lyrics, related content and credits.

from uuid import UUID

from fastapi import APIRouter, Depends, Response
from supabase import Client

from core.auth import get_current_user_id, get_user_db
from core.cache import CacheClient, get_redis
from core.cache_control import set_max_age, set_private_max_age
from core.exceptions import InvalidRequest
from core.search_provider import SearchProvider, get_search_provider
from models.playlists import LIKED_PLAYLIST_ID
from models.responses import ApiSuccess, ok_response
from models.track import (
    TrackCredits,
    TrackLyricsResult,
    TrackRelated,
    TrackUpNext,
    UpNextSource,
)
from services.track_service import (
    get_personal_upnext,
    get_track_credits,
    get_track_lyrics,
    get_track_related,
)

router = APIRouter(prefix="/tracks", tags=["tracks"])

# Same rule as Path(pattern=r"^MPRE") in routes/album.py and routes/public.py.
_ALBUM_ID_PREFIX = "MPRE"


def _upnext_source(
    source_kind: str | None = None, source_id: str | None = None
) -> UpNextSource | None:
    # One without the other is a malformed request (422), and so is an id
    # whose format does not fit its kind. A well-formed source that does not
    # exist or belongs to someone else is not an error: the service ignores
    # it. Like _status_filter in routes/bug_reports.py, this resolves before
    # the handler, so a 422 never reaches the provider or the database. The
    # format cannot be a typed query param because it depends on the kind;
    # the ValueError -> InvalidRequest translation is re-raised, not
    # swallowed, as in core/pagination.py. For `liked` the precedent is
    # RegisterRecentRequest._validate_kind.
    # An empty value counts as absent, for both params; the pair rule decides.
    if source_kind == "":
        source_kind = None
    if source_id == "":
        source_id = None
    if source_kind is None and source_id is None:
        return None
    if source_kind is None or source_id is None:
        raise InvalidRequest()

    match source_kind:
        case "album":
            if not source_id.startswith(_ALBUM_ID_PREFIX):
                raise InvalidRequest()
        case "playlist" | "genre_playlist":
            try:
                # Canonical form, so a spelling uuid.UUID accepts and
                # Postgres does not never reaches the query.
                source_id = str(UUID(source_id))
            except ValueError as exc:
                raise InvalidRequest() from exc
        case "liked":
            if source_id != LIKED_PLAYLIST_ID:
                raise InvalidRequest()
        case _:
            raise InvalidRequest()
    return UpNextSource(kind=source_kind, id=source_id)


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
    response: Response,
    # From the token, never from the query: it decides which plays and which
    # source are read.
    user_id: str = Depends(get_current_user_id),
    source: UpNextSource | None = Depends(_upnext_source),  # noqa: B008
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
    cache: CacheClient = Depends(get_redis),  # noqa: B008
    db: Client = Depends(get_user_db),  # noqa: B008
) -> ApiSuccess[TrackUpNext]:
    value, remaining = get_personal_upnext(
        provider, cache, db, user_id, track_id, source
    )
    set_private_max_age(response, remaining)
    return ok_response(value)


@router.get("/{track_id}/lyrics", response_model=ApiSuccess[TrackLyricsResult])
def get_track_lyrics_route(
    track_id: str,
    response: Response,
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
    cache: CacheClient = Depends(get_redis),  # noqa: B008
) -> ApiSuccess[TrackLyricsResult]:
    value, remaining = get_track_lyrics(provider, cache, track_id)
    set_max_age(response, remaining)
    return ok_response(value)


@router.get("/{track_id}/related", response_model=ApiSuccess[TrackRelated])
def get_track_related_route(
    track_id: str,
    response: Response,
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
    cache: CacheClient = Depends(get_redis),  # noqa: B008
) -> ApiSuccess[TrackRelated]:
    value, remaining = get_track_related(provider, cache, track_id)
    set_max_age(response, remaining)
    return ok_response(value)


@router.get("/{track_id}/credits", response_model=ApiSuccess[TrackCredits])
def get_track_credits_route(
    track_id: str,
    response: Response,
    user_id: str = Depends(get_current_user_id),
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
    cache: CacheClient = Depends(get_redis),  # noqa: B008
) -> ApiSuccess[TrackCredits]:
    value, remaining = get_track_credits(provider, cache, track_id)
    set_max_age(response, remaining)
    return ok_response(value)
