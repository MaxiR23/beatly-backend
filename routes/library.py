# INFO: Library endpoints.

from typing import Literal

from fastapi import APIRouter, Depends
from supabase import Client

from core.auth import get_current_user_id
from core.database import get_db
from core.pagination import PageRequest, page_params
from models.library import AddLibraryItemRequest, LibraryItem
from models.responses import ApiSuccess, Paginated, ok_response
from services.library_service import (
    add_library_item,
    list_library_items,
    remove_library_item,
)

router = APIRouter(prefix="/library", tags=["library"])


@router.get("", response_model=ApiSuccess[Paginated[LibraryItem]])
def get_library_items(
    sort: Literal["added_at", "title"] = "added_at",
    order: Literal["asc", "desc"] = "desc",
    page: PageRequest = Depends(page_params),  # noqa: B008
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[Paginated[LibraryItem]]:
    items, page_block = list_library_items(db, user_id, sort, order, page)
    return ok_response(Paginated(items=items, page=page_block))


@router.post("", response_model=ApiSuccess[LibraryItem])
def add_library_item_route(
    item: AddLibraryItemRequest,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[LibraryItem]:
    return ok_response(add_library_item(db, user_id, item))


@router.delete("/{kind}/{external_id}", response_model=ApiSuccess[None])
def remove_library_item_route(
    kind: Literal["album", "playlist"],
    external_id: str,
    user_id: str = Depends(get_current_user_id),
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[None]:
    remove_library_item(db, user_id, kind, external_id)
    return ok_response(None)
