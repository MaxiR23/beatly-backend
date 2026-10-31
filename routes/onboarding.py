# INFO: Onboarding endpoints.

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_profile, get_current_user_id, get_user_db
from core.cache import CacheClient, get_redis
from core.cache_control import private_no_cache
from core.database import get_db
from core.image_color_dependency import ImageColors, image_colors, with_image_colors
from core.pagination import PageRequest, page_params
from core.search_provider import SearchProvider, get_search_provider
from models.onboarding import (
    CompleteOnboardingRequest,
    OnboardingArtist,
    OnboardingCompletion,
    SimilarArtists,
    SimilarArtistsRequest,
)
from models.profiles import Profile
from models.responses import ApiSuccess, Paginated, ok_response
from services.onboarding_service import (
    complete_onboarding,
    list_onboarding_artists,
    similar_artists,
)

# Router-level on purpose, like the other user-data domains: the rule is per
# domain, not per method. Database clients: the curated table and the mark of
# the profile use the service-role client (get_db), because onboarding_artists
# has no privileges for the client roles (migration 049) and authenticated
# cannot update the column (migration 050); the user_id from the token,
# filtered in the query, is the only barrier there. The library is written
# with the caller's client (get_user_db), so RLS is a second barrier.
# /similar uses no database client: only the provider and the cache (the
# colors go through the shared dependency).
router = APIRouter(
    prefix="/onboarding",
    tags=["onboarding"],
    dependencies=[Depends(private_no_cache)],
)


@router.get(
    "/artists",
    response_model=ApiSuccess[Paginated[OnboardingArtist]],
    dependencies=[Depends(get_current_user_id)],
)
def get_onboarding_artists(
    page: PageRequest = Depends(page_params),  # noqa: B008
    db: Client = Depends(get_db),  # noqa: B008
    colors: ImageColors = Depends(image_colors),  # noqa: B008
) -> ApiSuccess[Paginated[OnboardingArtist]]:
    items, page_block = list_onboarding_artists(db, page)
    return with_image_colors(
        colors, ok_response(Paginated(items=items, page=page_block))
    )


@router.post("/complete", response_model=ApiSuccess[OnboardingCompletion])
def complete_onboarding_route(
    body: CompleteOnboardingRequest,
    profile: Profile = Depends(get_current_profile),  # noqa: B008
    user_db: Client = Depends(get_user_db),  # noqa: B008
    db: Client = Depends(get_db),  # noqa: B008
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
    cache: CacheClient = Depends(get_redis),  # noqa: B008
) -> ApiSuccess[OnboardingCompletion]:
    return ok_response(
        complete_onboarding(user_db, db, provider, cache, profile, body.artist_ids)
    )


@router.post(
    "/similar",
    response_model=ApiSuccess[SimilarArtists],
    dependencies=[Depends(get_current_user_id)],
)
def similar_artists_route(
    body: SimilarArtistsRequest,
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
    cache: CacheClient = Depends(get_redis),  # noqa: B008
    colors: ImageColors = Depends(image_colors),  # noqa: B008
) -> ApiSuccess[SimilarArtists]:
    return with_image_colors(
        colors,
        ok_response(similar_artists(provider, cache, body.artist_id, body.exclude_ids)),
    )
