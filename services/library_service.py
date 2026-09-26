# INFO: Reads the authenticated user's unified library (own playlists and
# saved items) and writes their saved library items in Supabase.

from supabase import Client

from core.exceptions import NotFound, UpstreamError
from core.pagination import PageRequest, SortKey, ValueType, apply_page, build_page
from core.upstream import translate_upstream_errors
from models.library import AddLibraryItemRequest, LibraryEntry, LibraryItem
from models.responses import PageBlock
from services.playlist_service import LIKED_PLAYLIST_ID

# row_id and added_at are not part of LibraryEntry -- build_page() reads
# both off each row to decode/encode cursors and to pick the tiebreaker,
# and they are dropped before the row reaches LibraryEntry(**row). user_id
# is not selected: it is only a filter here, never returned.
_ENTRY_COLUMNS = "row_id, kind, source, id, title, thumbnail_url, subtitle, added_at"

# A single order over the unified view: added_at desc, row_id as the
# tiebreaker. row_id is playlists.id or library_items.id depending on the
# row's branch in the view -- both gen_random_uuid() (017), so a uuid is
# unique within its own table and, in practice, never collides across the
# two. added_at on the playlists branch is created_at, which is nullable
# in 017 (playlists.created_at, line 1342) -- library_entries carries that
# assumption forward unchanged, the same one _LIST_SORT already makes for
# GET /playlists in playlist_service.py.
_ENTRIES_SORT = SortKey(
    "added_at",
    ValueType.TIMESTAMP,
    descending=True,
    id_column="row_id",
    id_type=ValueType.UUID,
)

# Same id and title GET /playlists/liked already returns for the virtual
# "liked songs" playlist -- that is where this entry opens on the client,
# so it has to carry the same identity, not a duplicate literal.
_LIKED_SOURCE = "liked"


def list_library_entries(
    db: Client, user_id: str, page: PageRequest
) -> tuple[list[LibraryEntry], PageBlock]:
    with translate_upstream_errors():
        # Decoded here, ahead of any db.table() call, so a bad cursor never
        # reaches the database — apply_page decodes it again below to build
        # the filter, but by then it is already known to be valid.
        page.decode(_ENTRIES_SORT)

        query = (
            db.table("library_entries")
            .select(_ENTRY_COLUMNS, count=page.count_mode)
            .eq("user_id", user_id)
        )
        query = apply_page(query, _ENTRIES_SORT, page)

        response = query.execute()

        rows, block = build_page(
            response.data or [], page, _ENTRIES_SORT, response.count
        )
        entries = [LibraryEntry(**row) for row in rows]

    # Outside translate_upstream_errors(): synthesizing the fixed entry is
    # not a database call. It is prepended to the items, not folded into
    # build_page(), so limit/total/has_more/next_cursor stay computed
    # purely off library_entries -- the fixed entry does not count against
    # limit or total (decisions 6/7 of plan-153), and only ever appears on
    # the first page (no cursor), never on a page reached via cursor.
    if page.is_first_page:
        liked_entry = LibraryEntry(
            kind="playlist",
            id=LIKED_PLAYLIST_ID,
            title=LIKED_PLAYLIST_ID,
            thumbnail_url=None,
            subtitle=None,
            source=_LIKED_SOURCE,
        )
        entries = [liked_entry, *entries]

    return entries, block


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
