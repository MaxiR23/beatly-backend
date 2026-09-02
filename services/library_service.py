# INFO: Reads and writes the authenticated user's library items in Supabase.

from supabase import Client

from core.exceptions import NotFound, UpstreamError
from core.pagination import PageRequest, SortKey, ValueType, apply_page, build_page
from core.upstream import translate_upstream_errors
from models.library import AddLibraryItemRequest, LibraryItem
from models.responses import PageBlock

_COLUMNS = (
    "id, kind, external_id, title, thumbnail_url, artist, artist_id, "
    "album_id, album_name, source, added_at, updated_at"
)

# One SortKey per sort/order combination the route accepts. descending=True
# is SortKey's own default, but all four are written explicit here so the
# table reads as the source of truth for what the route is allowed to ask
# for. None declares id_column or id_type: SortKey's default
# (id_column="id", id_type=ValueType.UUID) already matches library_items.id.
_SORT_KEYS = {
    ("added_at", "desc"): SortKey("added_at", ValueType.TIMESTAMP),
    ("added_at", "asc"): SortKey("added_at", ValueType.TIMESTAMP, descending=False),
    ("title", "desc"): SortKey("title", ValueType.TEXT),
    ("title", "asc"): SortKey("title", ValueType.TEXT, descending=False),
}


def list_library_items(
    db: Client, user_id: str, sort: str, order: str, page: PageRequest
) -> tuple[list[LibraryItem], PageBlock]:
    with translate_upstream_errors():
        # The route's Literal[...] already guarantees (sort, order) is one
        # of the four pairs below; a KeyError here can only come from the
        # route and this table drifting apart, which is a bug and correctly
        # surfaces as 502 upstream_error, not a silent empty page.
        sort_key = _SORT_KEYS[(sort, order)]

        # Decoded here, ahead of any db.table() call, so a bad cursor never
        # reaches the database — apply_page decodes it again below to build
        # the filter, but by then it is already known to be valid.
        page.decode(sort_key)

        query = (
            db.table("library_items")
            .select(_COLUMNS, count=page.count_mode)
            .eq("user_id", user_id)
        )
        query = apply_page(query, sort_key, page)

        response = query.execute()

        rows, block = build_page(response.data or [], page, sort_key, response.count)
        return [LibraryItem(**row) for row in rows], block


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

        # An upsert returning no row is an upstream anomaly. Indexing
        # blindly would raise IndexError, which is not translated, and
        # surface as a 500.
        if not response.data:
            raise UpstreamError()

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
