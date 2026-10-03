# INFO: Reads and writes the authenticated user's likes in Supabase.

from datetime import UTC, datetime

from supabase import Client

from core.exceptions import InvalidRequest, UpstreamError
from core.pagination import PageRequest, SortKey, ValueType, apply_page, build_page
from core.thumbnails import square_thumbnail_url
from core.upstream import translate_upstream_errors
from models.likes import AddLikeRequest, Like
from models.responses import PageBlock

# The track fields live in the shared catalog (public.tracks), not on
# user_likes. They are read through the relation (user_likes_track_id_fkey,
# many-to-one, so PostgREST embeds a single object).
_COLUMNS = (
    "track_id, created_at, updated_at, deleted_at, "
    "tracks(title, artists, album, album_id, thumbnail_url, duration_seconds)"
)

_TRACK_FIELDS = (
    "title",
    "artists",
    "album",
    "album_id",
    "thumbnail_url",
    "duration_seconds",
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

        return Like(**_flat_like(response.data[0]))


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
        return [Like(**_flat_like(row)) for row in rows], block


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
        return [Like(**_flat_like(row)) for row in rows], block


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


# Flattens a user_likes row with its embedded catalog track into the flat
# shape of Like, then rewrites thumbnail_url. Indexed on purpose: a null
# embed or a missing field is a TypeError/KeyError inside
# translate_upstream_errors(), a 502. It cannot happen, the FK cascades.
def _flat_like(row: dict) -> dict:
    track = row["tracks"]
    return _with_square_thumbnail(
        {
            "track_id": row["track_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
            **{field: track[field] for field in _TRACK_FIELDS},
        }
    )
