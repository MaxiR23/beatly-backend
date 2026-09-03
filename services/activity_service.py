# INFO: Reads and writes the authenticated user's plays and recents in Supabase.

from datetime import UTC, datetime

from supabase import Client

from core.exceptions import InvalidRequest, UpstreamError
from core.pagination import PageRequest, SortKey, ValueType, build_page
from core.upstream import translate_upstream_errors
from models.activity import (
    LogPlayRequest,
    PlayEvent,
    RecentEntity,
    RegisterRecentRequest,
)
from models.responses import PageBlock

_RECENT_COLUMNS = "entity_type, entity_id, metadata, played_at"

# Recents are never trimmed: the table keeps every row and the read caps
# the result instead. Trimming old rows is deferred to its own issue. The
# cap now bounds the requested limit rather than being the query's own
# .limit(): a client asking for more than this still gets a single page of
# at most this many rows.
_RECENTS_CAP = 30

# Declares the ORDER BY in one place and is what build_page requires. The id
# tiebreaker does not serve a cursor here — this endpoint never emits one —
# it makes the cut at _RECENTS_CAP deterministic when several rows share the
# same played_at.
_RECENTS_SORT = SortKey(
    "played_at",
    ValueType.TIMESTAMP,
    descending=True,
    id_column="id",
    id_type=ValueType.UUID,
)


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


def list_recents(
    db: Client, user_id: str, page: PageRequest
) -> tuple[list[RecentEntity], PageBlock]:
    # Outside the block below, and before any db.table() call: this
    # endpoint answers in a single page and never emits a cursor, so one
    # received here could not have come from it. Rejecting it is a decision
    # about the request, not a database failure — same reasoning as
    # move_track.
    if not page.is_first_page:
        raise InvalidRequest("invalid_cursor")

    # The cap bounds the limit, not the collection: asking for more than 30
    # is not an error, it returns 30. This PageRequest drives both the query
    # and the block, so the three numbers can never disagree.
    capped = PageRequest(limit=min(page.limit, _RECENTS_CAP))

    with translate_upstream_errors():
        response = (
            db.table("recent_activity")
            .select(_RECENT_COLUMNS, count=capped.count_mode)
            .eq("user_id", user_id)
            .order(_RECENTS_SORT.column, desc=_RECENTS_SORT.descending)
            .order(_RECENTS_SORT.id_column, desc=_RECENTS_SORT.descending)
            # limit and not fetch_limit: the probe row exists to answer
            # has_more, and there is no next page to announce here.
            .limit(capped.limit)
            .execute()
        )

        # Exact relative to what this endpoint exposes -the 30 newest- not
        # the table: a raw count would describe rows no request can reach.
        # A missing count is left as None on purpose, so build_page rejects
        # it as the bug it is (500) instead of reporting total: 0.
        total = response.count
        if total is not None:
            total = min(total, _RECENTS_CAP)

        # has_more and next_cursor come out false/null by derivation: never
        # more rows are requested than capped.limit.
        rows, block = build_page(response.data or [], capped, _RECENTS_SORT, total)
        return [RecentEntity(**row) for row in rows], block
