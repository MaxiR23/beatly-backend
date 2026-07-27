# INFO: Reads and writes the authenticated user's likes in Supabase.

from datetime import UTC, datetime

from supabase import Client

from core.exceptions import ResourceEmpty
from core.upstream import translate_upstream_errors
from models.likes import AddLikeRequest, Like, LikeList

_COLUMNS = (
    "track_id, title, artists, album, album_id, thumbnail_url, "
    "duration_seconds, created_at, updated_at, deleted_at"
)


def like_track(db: Client, user_id: str, item: AddLikeRequest) -> Like:
    with translate_upstream_errors():
        payload = {**item.model_dump(), "user_id": user_id, "deleted_at": None}

        response = (
            db.table("user_likes")
            .upsert(payload, on_conflict="user_id,track_id")
            .execute()
        )

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


def list_likes(db: Client, user_id: str) -> LikeList:
    with translate_upstream_errors():
        response = (
            db.table("user_likes")
            .select(_COLUMNS)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .order("created_at")
            .execute()
        )

        if not response.data:
            raise ResourceEmpty("no_likes")

        return LikeList(likes=[Like(**row) for row in response.data])


def sync_likes(db: Client, user_id: str, since: datetime) -> LikeList:
    with translate_upstream_errors():
        response = (
            db.table("user_likes")
            .select(_COLUMNS)
            .eq("user_id", user_id)
            .gt("updated_at", since.isoformat())
            .order("updated_at")
            .execute()
        )

        return LikeList(likes=[Like(**row) for row in response.data])
