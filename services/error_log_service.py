# INFO: Records the client's playback errors in Supabase, with per-user dedup and cap.

import logging
from datetime import UTC, datetime, timedelta

from supabase import Client

from core.exceptions import RateLimited, UpstreamError
from core.upstream import translate_upstream_errors
from models.errors import PlaybackErrorRequest

logger = logging.getLogger(__name__)

# A report for the same user, track and stage within this window is dropped.
_DEDUP_WINDOW = timedelta(seconds=5)
# At most _CAP_ROWS stored reports per user in this rolling window.
_CAP_WINDOW = timedelta(minutes=10)
_CAP_ROWS = 20


def record_playback_error(
    db: Client, user_id: str, payload: PlaybackErrorRequest
) -> None:
    # One clock for writing created_at and for both windows: the database's
    # or the Docker VM's clock can drift from this one (see docs/testing.md).
    now = datetime.now(UTC)

    with translate_upstream_errors():
        # get_db (service-role) skips RLS on purpose: an owner INSERT policy
        # would let a client write error_logs directly and bypass the cap.
        # Isolation is these filters alone, so each starts with user_id.
        duplicate = (
            db.table("error_logs")
            .select("id")
            .eq("user_id", user_id)
            .eq("track_id", payload.track_id)
            .eq("stage", payload.stage)
            .gte("created_at", (now - _DEDUP_WINDOW).isoformat())
            .limit(1)
            .execute()
        )
        if duplicate.data:
            logger.info(
                "playback error deduplicated user_id=%s stage=%s track_id=%r",
                user_id,
                payload.stage,
                payload.track_id,
            )
            return

        recent = (
            db.table("error_logs")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .gte("created_at", (now - _CAP_WINDOW).isoformat())
            .limit(1)
            .execute()
        )
        count = recent.count
        # A missing count is not read as 0: that would lift the cap.
        if isinstance(count, bool) or not isinstance(count, int):
            raise UpstreamError()
        # Two simultaneous requests can pass the cap by a few rows; the race
        # is accepted (#191).
        if count >= _CAP_ROWS:
            logger.warning("playback error over the cap user_id=%s", user_id)
            raise RateLimited()

        response = (
            db.table("error_logs")
            .insert(
                {
                    **payload.model_dump(),
                    "user_id": user_id,
                    "created_at": now.isoformat(),
                }
            )
            .execute()
        )
        if not response.data:
            raise UpstreamError()
