# INFO: Reads and writes the authenticated user's likes in Supabase.

from datetime import UTC, datetime

from supabase import Client

from core.exceptions import UpstreamError
from core.pagination import PageRequest, SortKey, ValueType, apply_page, build_page
from core.upstream import translate_upstream_errors
from models.likes import AddLikeRequest, Like
from models.responses import PageBlock

_COLUMNS = (
    "track_id, title, artists, album, album_id, thumbnail_url, "
    "duration_seconds, created_at, updated_at, deleted_at"
)

_LIST_SORT = SortKey(
    "created_at",
    ValueType.TIMESTAMP,
    descending=False,
    id_column="track_id",
    id_type=ValueType.TEXT,
)
_SYNC_SORT = SortKey(
    "updated_at",
    ValueType.TIMESTAMP,
    descending=False,
    id_column="track_id",
    id_type=ValueType.TEXT,
)


def like_track(db: Client, user_id: str, item: AddLikeRequest) -> Like:
    with translate_upstream_errors():
        payload = {**item.model_dump(), "user_id": user_id, "deleted_at": None}

        response = (
            db.table("user_likes")
            .upsert(payload, on_conflict="user_id,track_id")
            .execute()
        )

        # An upsert returning no row is an upstream anomaly. Indexing
        # blindly would raise IndexError, which is not translated, and
        # surface as a 500.
        if not response.data:
            raise UpstreamError()

        return Like(**response.data[0])


def unlike_track(db: Client, user_id: str, track_id: str) -> None:
    with translate_upstream_errors():
        now = datetime.now(UTC).isoformat()

        (
            db.table("user_likes")
            .update({"deleted_at": now})
            .eq("user_id", user_id)
            .eq("track_id", track_id)
            .execute()
        )


def list_likes(
    db: Client, user_id: str, page: PageRequest
) -> tuple[list[Like], PageBlock]:
    with translate_upstream_errors():
        # Decoded here, ahead of any db.table() call, so a bad cursor never
        # reaches the database — apply_page decodes it again below to build
        # the filter, but by then it is already known to be valid.
        page.decode(_LIST_SORT)

        query = (
            db.table("user_likes")
            .select(_COLUMNS, count=page.count_mode)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
        )
        query = apply_page(query, _LIST_SORT, page)

        response = query.execute()

        rows, block = build_page(response.data or [], page, _LIST_SORT, response.count)
        return [Like(**row) for row in rows], block


def sync_likes(
    db: Client, user_id: str, since: datetime | None, page: PageRequest
) -> tuple[list[Like], PageBlock]:
    with translate_upstream_errors():
        # See list_likes: decoded early so a bad cursor never reaches the
        # database.
        page.decode(_SYNC_SORT)

        query = (
            db.table("user_likes")
            .select(_COLUMNS, count=page.count_mode)
            .eq("user_id", user_id)
        )
        if page.is_first_page and since is not None:
            query = query.gt("updated_at", since.isoformat())
        query = apply_page(query, _SYNC_SORT, page)

        response = query.execute()

        rows, block = build_page(response.data or [], page, _SYNC_SORT, response.count)
        return [Like(**row) for row in rows], block
