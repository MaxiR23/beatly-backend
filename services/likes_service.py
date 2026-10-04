# INFO: Reads and writes the authenticated user's likes in Supabase.

from datetime import UTC, datetime

from supabase import Client

from core.exceptions import InvalidRequest, UpstreamError
from core.pagination import (
    PageRequest,
    SortKey,
    ValueType,
    apply_page,
    build_page,
    is_checkpoint,
)
from core.thumbnails import square_thumbnail_url
from core.upstream import translate_upstream_errors
from models.likes import AddLikeRequest, Like
from models.responses import PageBlock

# The track fields live in the shared catalog (public.tracks), not on
# user_likes. The relation (user_likes_track_id_fkey) is many-to-one, so it is
# read with the PostgREST spread `...`, which returns the track fields as keys
# of the like row: the flat shape of Like.
# SEE: https://docs.postgrest.org/en/stable/references/api/resource_embedding.html
# If the relation were null the six fields arrive as null and Like rejects it
# inside translate_upstream_errors() (502); it cannot happen, the FK cascades.
_COLUMNS = (
    "track_id, created_at, updated_at, deleted_at, "
    "...tracks(title, artists, album, album_id, thumbnail_url, duration_seconds)"
)

_LIST_SORT = SortKey(
    "created_at",
    ValueType.TIMESTAMP,
    descending=False,
    id_column="track_id",
    id_type=ValueType.TEXT,
    carries_checkpoint=True,
)
_SYNC_SORT = SortKey(
    "updated_at",
    ValueType.TIMESTAMP,
    descending=False,
    id_column="track_id",
    id_type=ValueType.TEXT,
    carries_checkpoint=True,
)

# Side of the square image the stored thumbnail_url is rewritten to when it
# is read, with smart crop. It is never rewritten when written: what is
# stored stays as the client sent it, so old and new rows are both covered.
_THUMBNAIL_SIZE = 544


def like_track(
    db: Client, catalog_db: Client, user_id: str, item: AddLikeRequest
) -> Like:
    # Exactly the seven columns of tracks that _upsert_tracks writes.
    track = item.model_dump()

    if track["duration_seconds"] is None:
        # Read first: an upsert that leaves the key out fails with 23502
        # even when the row exists, because Postgres checks NOT NULL on the
        # proposed row before it resolves ON CONFLICT.
        with translate_upstream_errors():
            existing = (
                catalog_db.table("tracks")
                .select("duration_seconds")
                .eq("track_id", item.track_id)
                .execute()
            )
            duration = existing.data[0]["duration_seconds"] if existing.data else None

        if duration is None:
            raise InvalidRequest()
        track["duration_seconds"] = duration

    with translate_upstream_errors():
        # Service-role because tracks has no write policy for authenticated,
        # like _upsert_tracks; and before the like, because of the FK.
        catalog = catalog_db.table("tracks").upsert(track, on_conflict="track_id")
        if not catalog.execute().data:
            raise UpstreamError()

        response = (
            db.table("user_likes")
            .upsert(
                {"user_id": user_id, "track_id": item.track_id, "deleted_at": None},
                on_conflict="user_id,track_id",
            )
            .select(_COLUMNS)
            .execute()
        )

        # An upsert returning no row is an upstream anomaly. Indexing
        # blindly would raise IndexError, which is not translated, and
        # surface as a 500.
        if not response.data:
            raise UpstreamError()

        return Like(**_with_square_thumbnail(response.data[0]))


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


# The database clock minus the overlap, read through an RPC because PostgREST
# cannot expose now() from a table query. Called inside the caller's
# translate_upstream_errors(). A response that is not a timestamp with a zone
# is an upstream anomaly (502), never a made-up checkpoint: an invented one
# would lose changes.
def _read_checkpoint(db: Client) -> str:
    response = db.rpc("likes_sync_checkpoint", {}).execute()

    if not is_checkpoint(response.data):
        raise UpstreamError()

    return response.data


def list_likes(
    db: Client, user_id: str, page: PageRequest
) -> tuple[list[Like], PageBlock, str]:
    with translate_upstream_errors():
        # Decoded here, ahead of any db.table() call and ahead of the
        # checkpoint RPC, so a bad cursor never reaches the database —
        # apply_page decodes it again below to build the filter, but by then
        # it is already known to be valid.
        cursor = page.decode(_LIST_SORT)

        # First page: read the clock before the data. Later pages: the one
        # the cursor carries, so every page of a read returns the same value.
        checkpoint = cursor.checkpoint if cursor is not None else _read_checkpoint(db)

        query = (
            db.table("user_likes")
            .select(_COLUMNS, count=page.count_mode)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
        )
        query = apply_page(query, _LIST_SORT, page)

        response = query.execute()

        rows, block = build_page(
            response.data or [], page, _LIST_SORT, response.count, checkpoint
        )
        return [Like(**_with_square_thumbnail(row)) for row in rows], block, checkpoint


def sync_likes(
    db: Client, user_id: str, since: datetime | None, page: PageRequest
) -> tuple[list[Like], PageBlock, str]:
    with translate_upstream_errors():
        # See list_likes: decoded early so a bad cursor never reaches the
        # database or the checkpoint RPC.
        cursor = page.decode(_SYNC_SORT)
        checkpoint = cursor.checkpoint if cursor is not None else _read_checkpoint(db)

        query = (
            db.table("user_likes")
            .select(_COLUMNS, count=page.count_mode)
            .eq("user_id", user_id)
        )
        if page.is_first_page and since is not None:
            query = query.gt("updated_at", since.isoformat())
        query = apply_page(query, _SYNC_SORT, page)

        response = query.execute()

        rows, block = build_page(
            response.data or [], page, _SYNC_SORT, response.count, checkpoint
        )
        return [Like(**_with_square_thumbnail(row)) for row in rows], block, checkpoint


# Returns a copy of the row with thumbnail_url rewritten. The key is indexed
# on purpose: it is in every select, and if it were missing the KeyError is
# a 502 inside translate_upstream_errors(). It is a copy in each database
# service, not an import, like the other private mapping helpers.
def _with_square_thumbnail(row: dict) -> dict:
    return {
        **row,
        "thumbnail_url": square_thumbnail_url(
            row["thumbnail_url"], _THUMBNAIL_SIZE, smart_crop=True
        ),
    }
