# INFO: Reads and writes the authenticated user's plays and recents in Supabase.

from datetime import UTC, datetime

from supabase import Client

from core.exceptions import ResourceEmpty, UpstreamError
from core.upstream import translate_upstream_errors
from models.activity import (
    LogPlayRequest,
    PlayEvent,
    RecentEntity,
    RecentEntityList,
    RegisterRecentRequest,
)

_RECENT_COLUMNS = "entity_type, entity_id, metadata, played_at"

# Recents are never trimmed: the table keeps every row and the read caps
# the result instead. Trimming old rows is deferred to its own issue.
_RECENTS_LIMIT = 30


def log_play(db: Client, user_id: str, item: LogPlayRequest) -> PlayEvent:
    with translate_upstream_errors():
        payload = {
            "user_id": user_id,
            "track_id": item.track_id,
            "metadata": item.model_dump(exclude={"track_id"}),
            "played_at": datetime.now(UTC).isoformat(),
        }

        response = db.table("play_events").insert(payload).execute()

        # An insert returning no row is an upstream anomaly. Indexing
        # blindly would raise IndexError, which is not translated, and
        # surface as a 500.
        if not response.data:
            raise UpstreamError()

        return PlayEvent(**response.data[0])


def register_recent(
    db: Client, user_id: str, item: RegisterRecentRequest
) -> RecentEntity:
    with translate_upstream_errors():
        payload = {
            **item.model_dump(),
            "user_id": user_id,
            "played_at": datetime.now(UTC).isoformat(),
        }

        response = (
            db.table("recent_activity")
            .upsert(payload, on_conflict="user_id,entity_type,entity_id")
            .execute()
        )

        # An upsert returning no row is an upstream anomaly. Indexing
        # blindly would raise IndexError, which is not translated, and
        # surface as a 500.
        if not response.data:
            raise UpstreamError()

        return RecentEntity(**response.data[0])


def list_recents(db: Client, user_id: str) -> RecentEntityList:
    with translate_upstream_errors():
        response = (
            db.table("recent_activity")
            .select(_RECENT_COLUMNS)
            .eq("user_id", user_id)
            .order("played_at", desc=True)
            .limit(_RECENTS_LIMIT)
            .execute()
        )

        if not response.data:
            raise ResourceEmpty("no_recents")

        return RecentEntityList(items=[RecentEntity(**row) for row in response.data])
