# INFO: Reads and writes the authenticated user's library items in Supabase.

from supabase import Client

from core.exceptions import NotFound, ResourceEmpty
from core.upstream import translate_upstream_errors
from models.library import AddLibraryItemRequest, LibraryItem, LibraryItemList

_COLUMNS = (
    "kind, external_id, title, thumbnail_url, artist, artist_id, "
    "album_id, album_name, source, added_at, updated_at"
)


def list_library_items(
    db: Client, user_id: str, sort: str, order: str
) -> LibraryItemList:
    with translate_upstream_errors():
        response = (
            db.table("library_items")
            .select(_COLUMNS)
            .eq("user_id", user_id)
            .order(sort, desc=(order == "desc"))
            .execute()
        )

        if not response.data:
            raise ResourceEmpty("no_library_items")

        return LibraryItemList(items=[LibraryItem(**row) for row in response.data])


def add_library_item(
    db: Client, user_id: str, item: AddLibraryItemRequest
) -> LibraryItem:
    with translate_upstream_errors():
        payload = {**item.model_dump(), "user_id": user_id}

        response = (
            db.table("library_items")
            .upsert(payload, on_conflict="user_id,kind,external_id")
            .execute()
        )

        return LibraryItem(**response.data[0])


def remove_library_item(db: Client, user_id: str, kind: str, external_id: str) -> None:
    with translate_upstream_errors():
        response = (
            db.table("library_items")
            .delete()
            .eq("user_id", user_id)
            .eq("kind", kind)
            .eq("external_id", external_id)
            .execute()
        )

        if not response.data:
            raise NotFound("library_item_not_found")
