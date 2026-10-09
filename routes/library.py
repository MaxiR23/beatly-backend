# INFO: Library endpoints.

from typing import Literal

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id, get_user_db
from core.cache_control import private_no_cache
from core.pagination import PageRequest, page_params
from models.library import (
    AddLibraryItemRequest,
    LibraryEntry,
    LibraryItem,
    LibraryItemSavedState,
)
from models.responses import ApiSuccess, Paginated, ok_response
from services.library_service import (
    add_library_item,
    get_library_item_saved_state,
    list_library_entries,
    remove_library_item,
)

# Router-level on purpose: the rule is per domain, not per method, so any
# new endpoint in these domains inherits it without remembering to opt in.
router = APIRouter(
    prefix="/library", tags=["library"], dependencies=[Depends(private_no_cache)]
)


@router.get("", response_model=ApiSuccess[Paginated[LibraryEntry]])
def get_library_items(
    page: PageRequest = Depends(page_params),  # noqa: B008
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_user_db),  # noqa: B008
) -> ApiSuccess[Paginated[LibraryEntry]]:
    items, page_block = list_library_entries(db, user_id, page)
    return ok_response(Paginated(items=items, page=page_block))


@router.post("", response_model=ApiSuccess[LibraryItem])
def add_library_item_route(
    item: AddLibraryItemRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_user_db),  # noqa: B008
) -> ApiSuccess[LibraryItem]:
    return ok_response(add_library_item(db, user_id, item))


@router.get("/{kind}/{external_id}", response_model=ApiSuccess[LibraryItemSavedState])
def get_library_item_saved_state_route(
    kind: Literal["album", "playlist"],
    external_id: str,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_user_db),  # noqa: B008
) -> ApiSuccess[LibraryItemSavedState]:
    return ok_response(get_library_item_saved_state(db, user_id, kind, external_id))


@router.delete("/{kind}/{external_id}", response_model=ApiSuccess[None])
def remove_library_item_route(
    kind: Literal["album", "playlist"],
    external_id: str,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_user_db),  # noqa: B008
) -> ApiSuccess[None]:
    remove_library_item(db, user_id, kind, external_id)
    return ok_response(None)
