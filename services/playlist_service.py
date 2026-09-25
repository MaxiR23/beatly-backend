# INFO: Reads and writes the authenticated user's playlists, and reads a public playlist by id with no user, in Supabase.

from datetime import UTC, datetime

from fractional_indexing import generate_key_between, generate_n_keys_between
from supabase import Client

from core.exceptions import Conflict, InvalidRequest, NotFound, UpstreamError
from core.pagination import (
    Cursor,
    PageRequest,
    SortKey,
    ValueType,
    apply_page,
    build_page,
    through_cursor_filter,
)
from core.upstream import translate_upstream_errors
from models.playlists import (
    AddPlaylistTrackRequest,
    BulkAddPlaylistTracksRequest,
    BulkAddResult,
    CreatePlaylistRequest,
    MovePlaylistTrackRequest,
    OwnedPlaylistIds,
    Playlist,
    PlaylistDetail,
    PlaylistPageTrack,
    PlaylistTrack,
    PlaylistWithTracks,
    UpdatePlaylistRequest,
)
from models.responses import PageBlock

_COLUMNS = "id, owner_id, title, description, is_public, created_at, updated_at"

# created_at and not updated_at: trg_bump_playlist_updated_at
# (db/migrations/009_triggers.sql) moves updated_at whenever title,
# description or is_public actually change, and
# trg_bump_playlist_on_track_change moves it on every track add or remove,
# so a playlist edited mid-walk would jump between pages. created_at is
# immutable. Neither id_column nor id_type is declared: SortKey's defaults
# (id_column="id", id_type=ValueType.UUID) already match playlists.id.
_LIST_SORT = SortKey("created_at", ValueType.TIMESTAMP)

# order_key is unique per playlist since 028 (ux_playlist_order_key on
# (playlist_id, order_key)), NOT NULL, COLLATE "C", and no add rewrites it
# once written -- only move_track does, and only for the moved row. Neither
# id_column nor id_type is declared: SortKey's defaults (id_column="id",
# id_type=ValueType.UUID) already match playlist_tracks.id.
_TRACKS_SORT = SortKey("order_key", ValueType.TEXT, descending=False)

# The same order GET /likes already returns (services/likes_service.py's
# _LIST_SORT), which is why a cursor from GET /likes decodes here too --
# they are the same sort key over the same rows.
_LIKED_SORT = SortKey(
    "created_at",
    ValueType.TIMESTAMP,
    descending=False,
    id_column="track_id",
    id_type=ValueType.TEXT,
)

_TRACK_COLUMNS = (
    "id, track_id, title, artists, album, album_id, duration_seconds, thumbnail_url"
)

# An explicit cap, not pagination: used only by get_public_playlist(), via
# _list_playlist_tracks() below, for GET /public/playlists/{id} -- the
# owner's own endpoints paginate for real, see list_playlist_tracks().
_TRACKS_LIMIT = 1000

# in_ goes into the query string, so a full playlist's worth of uuids in
# one filter builds a URI Supabase rejects. Fetch them in batches.
_TRACK_BATCH_SIZE = 150

# Both id and title of the virtual "liked songs" playlist. title is
# deliberately not a display string: Playlist.title is non-nullable, and
# the client resolves the visible name with i18n.
_LIKED_PLAYLIST_ID = "liked"

# How many times a write retries a fresh order_key after the RPC reports
# order_key_conflict, reading the neighbours again each time, no backoff:
# the RPC's own row lock (write protocol of 013) already serializes every
# writer on this playlist, so a retry only ever competes with a write that
# landed in the same gap between this service's read and its call -- a
# narrow window, not a queue to wait out.
_ORDER_KEY_ATTEMPTS = 3

# Part of the public share DTO's contract, not this RPC's default: the
# share card's mosaic is 4 tiles, and that number must be visible in the
# Python call, not inherited silently from get_user_playlist_thumbnails'
# own DEFAULT 4.
_THUMBNAILS_PER_PLAYLIST = 4


def can_edit(user_id: str, playlist: Playlist) -> bool:
    # The single definition of "may modify this playlist". Collaborative
    # playlists become a change here, not at every call site.
    return playlist.owner_id == user_id


def _get_editable_playlist(db: Client, user_id: str, playlist_id: str) -> Playlist:
    with translate_upstream_errors():
        response = (
            db.table("playlists").select(_COLUMNS).eq("id", playlist_id).execute()
        )

        if not response.data:
            raise NotFound("playlist_not_found")

        playlist = Playlist(**response.data[0])

    # Deliberately outside the block above: a permission decision is not a
    # database call, so a bug here must not be reported as upstream_error.
    # An existing but non-editable playlist gets the same 404 as an unknown
    # one, so the response does not confirm that it exists.
    if not can_edit(user_id, playlist):
        raise NotFound("playlist_not_found")

    return playlist


def _tracks_by(db: Client, column: str, values: list[str]) -> dict[str, dict]:
    # in_ goes into the query string, so a full batch's worth of ids in one
    # filter can build a URI Supabase rejects. Fetched in batches of
    # _TRACK_BATCH_SIZE and merged into one lookup, keyed on whichever
    # column the caller joined on: tracks.id for playlist_tracks (a uuid),
    # tracks.track_id for user_likes (the provider id).
    tracks_by_value = {}
    for start in range(0, len(values), _TRACK_BATCH_SIZE):
        batch = values[start : start + _TRACK_BATCH_SIZE]
        response = (
            db.table("tracks").select(_TRACK_COLUMNS).in_(column, batch).execute()
        )
        tracks_by_value.update({row[column]: row for row in response.data})
    return tracks_by_value


def _count_through(query: object, sort: SortKey, cursor: Cursor) -> int:
    # How many rows are at or before the cursor row, in the order of sort:
    # a page's own preceding count, so its items can carry a global 1-based
    # position instead of restarting at 1 (#139). query already carries
    # select(<id column>, count="exact") and the scope filters; only the
    # complement of the keyset filter and the probe limit are added here.
    response = query.or_(through_cursor_filter(sort, cursor)).limit(1).execute()

    count = response.count
    # A missing or boolean count is not silently treated as 0: that would
    # run every position on this page from 1 instead of surfacing the
    # anomaly, the same reasoning _duration_total uses for the RPC's total.
    if isinstance(count, bool) or not isinstance(count, int):
        raise UpstreamError()

    return count


def list_playlist_tracks(
    db: Client, user_id: str, playlist_id: str, page: PageRequest
) -> tuple[list[PlaylistPageTrack], PageBlock]:
    # Decoded first, ahead of any db call including the playlist lookup:
    # a bad cursor must never reach the database, and this is the one
    # paginated endpoint in this domain where the lookup itself is a query.
    cursor = page.decode(_TRACKS_SORT)

    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        preceding = 0
        if cursor is not None:
            preceding = _count_through(
                db.table("playlist_tracks")
                .select("id", count="exact")
                .eq("playlist_id", playlist_id),
                _TRACKS_SORT,
                cursor,
            )

        query = (
            db.table("playlist_tracks")
            .select("id, track_id, order_key", count=page.count_mode)
            .eq("playlist_id", playlist_id)
        )
        query = apply_page(query, _TRACKS_SORT, page)
        response = query.execute()

        rows, block = build_page(
            response.data or [], page, _TRACKS_SORT, response.count
        )

        tracks_by_id = _tracks_by(db, "id", [row["track_id"] for row in rows])

        # A track_id missing from the catalog cannot happen: the foreign
        # key cascades on delete. If it ever did, the KeyError becomes a
        # 502, same as _list_playlist_tracks.
        items = [
            PlaylistPageTrack(
                **tracks_by_id[row["track_id"]], position=preceding + index
            )
            for index, row in enumerate(rows, start=1)
        ]
        return items, block


def list_liked_playlist_tracks(
    db: Client, user_id: str, page: PageRequest
) -> tuple[list[PlaylistPageTrack], PageBlock]:
    # No parent to check: for an authenticated user this virtual playlist
    # always exists, even empty, same as get_liked_playlist().
    cursor = page.decode(_LIKED_SORT)

    with translate_upstream_errors():
        preceding = 0
        if cursor is not None:
            preceding = _count_through(
                db.table("user_likes")
                .select("track_id", count="exact")
                .eq("user_id", user_id)
                .is_("deleted_at", "null"),
                _LIKED_SORT,
                cursor,
            )

        query = (
            db.table("user_likes")
            .select("track_id, created_at", count=page.count_mode)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
        )
        query = apply_page(query, _LIKED_SORT, page)
        response = query.execute()

        rows, block = build_page(response.data or [], page, _LIKED_SORT, response.count)

        tracks_by_id = _tracks_by(db, "track_id", [row["track_id"] for row in rows])

        # A track_id missing from the catalog cannot happen: the foreign
        # key cascades on delete. If it ever did, the KeyError becomes a
        # 502, same as _list_liked_tracks used to.
        items = [
            PlaylistPageTrack(
                **tracks_by_id[row["track_id"]], position=preceding + index
            )
            for index, row in enumerate(rows, start=1)
        ]
        return items, block


def list_playlist_track_ids(
    db: Client, user_id: str, playlist_id: str, page: PageRequest
) -> tuple[list[str], PageBlock]:
    # Decoded first, ahead of any db call including the playlist lookup, same
    # reasoning as list_playlist_tracks: a bad cursor must never reach the
    # database. The decoded value is unused here; apply_page decodes it again
    # below to build the filter.
    page.decode(_TRACKS_SORT)

    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        query = (
            db.table("playlist_tracks")
            .select("id, track_id, order_key", count=page.count_mode)
            .eq("playlist_id", playlist_id)
        )
        query = apply_page(query, _TRACKS_SORT, page)
        response = query.execute()

        rows, block = build_page(
            response.data or [], page, _TRACKS_SORT, response.count
        )

        # playlist_tracks.track_id is a uuid referencing tracks.id, so it is
        # resolved to the provider id here, same as list_playlist_tracks. A
        # track_id missing from the catalog cannot happen: the foreign key
        # cascades on delete. If it ever did, the KeyError becomes a 502,
        # same as /tracks. No _count_through here: this endpoint carries no
        # position.
        tracks_by_id = _tracks_by(db, "id", [row["track_id"] for row in rows])
        items = [tracks_by_id[row["track_id"]]["track_id"] for row in rows]

        return items, block


def list_liked_playlist_track_ids(
    db: Client, user_id: str, page: PageRequest
) -> tuple[list[str], PageBlock]:
    # No parent to check: for an authenticated user this virtual playlist
    # always exists, even empty, same as list_liked_playlist_tracks.
    page.decode(_LIKED_SORT)

    with translate_upstream_errors():
        query = (
            db.table("user_likes")
            .select("track_id, created_at", count=page.count_mode)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
        )
        query = apply_page(query, _LIKED_SORT, page)
        response = query.execute()

        rows, block = build_page(response.data or [], page, _LIKED_SORT, response.count)

        # user_likes.track_id is already the provider id (it references
        # tracks.track_id, not tracks.id), so it is returned directly with
        # no lookup against the catalog at all.
        return [row["track_id"] for row in rows], block


def _list_playlist_tracks(
    db: Client, playlist_id: str
) -> tuple[list[PlaylistTrack], int]:
    with translate_upstream_errors():
        entries_response = (
            db.table("playlist_tracks")
            .select("track_id", count="exact")
            .eq("playlist_id", playlist_id)
            .order("order_key")
            .limit(_TRACKS_LIMIT)
            .execute()
        )

        total_count = entries_response.count or 0

        # An empty playlist is not an empty response: the playlist itself
        # is the payload, unlike GET /genre-playlists/{id}/tracks where the
        # track list is.
        if not entries_response.data:
            return [], total_count

        ordered_ids = [row["track_id"] for row in entries_response.data]

        # playlist_tracks.track_id is a uuid referencing tracks.id, not the
        # text tracks.track_id the curated genre path joins on.
        tracks_by_id = _tracks_by(db, "id", ordered_ids)

        # position is the 1-based index in this order_key order, not a
        # stored column -- playlist_tracks.position no longer exists (#137).
        # A missing id here cannot happen: the foreign key cascades on
        # delete. If it ever did, the KeyError becomes a 502.
        tracks = [
            PlaylistTrack(**tracks_by_id[track_id], position=position)
            for position, track_id in enumerate(ordered_ids, start=1)
        ]

        return tracks, total_count


def _upsert_tracks(db: Client, items: list[AddPlaylistTrackRequest]) -> dict[str, str]:
    # tracks.track_id is unique (tracks_track_id_key), so a track already in
    # the catalog has its metadata refreshed rather than duplicated. Returns
    # provider id -> catalog uuid, the id playlist_tracks joins on. Called
    # with the service-role client: public.tracks has no write policy for
    # authenticated (B1, #143), so the catalog upsert cannot go through the
    # caller's own JWT the way the rest of this service's writes do.
    response = (
        db.table("tracks")
        .upsert([item.model_dump() for item in items], on_conflict="track_id")
        .execute()
    )

    if not response.data:
        raise UpstreamError()

    return {row["track_id"]: row["id"] for row in response.data}


def _is_valid_position(position: int, track_count: int) -> bool:
    return 1 <= position <= track_count


def _rpc_payload(response: object) -> dict:
    # The playlist RPCs report their own failures in the payload rather than
    # as an error status — {"ok": true, ...} or {"ok": false, "error": ...} —
    # so a successful round trip can still mean the function refused.
    # supabase-py hands the returned json back as-is, which is a bare object
    # for a scalar return and a one-element list for a set one.
    data = getattr(response, "data", None)

    if isinstance(data, list):
        data = data[0] if data else None

    if not isinstance(data, dict):
        raise UpstreamError()

    return data


def _is_order_key_conflict(response: object) -> bool:
    # The RPC's own way of reporting that the order_key it was given
    # collided with, or landed on the wrong side of, another row under its
    # lock (ux_playlist_order_key, migration 028) -- same envelope key
    # ("error", not "reason") as track_already_in_playlist. A payload that
    # is not a dict has already raised UpstreamError inside _rpc_payload.
    return _rpc_payload(response).get("error") == "order_key_conflict"


def _rpc_result(response: object) -> dict:
    # For an RPC whose every refusal is an upstream anomaly. add_playlist_track
    # is not one of those: one of its errors is a domain answer.
    data = _rpc_payload(response)

    if not data.get("ok"):
        raise UpstreamError()

    return data


def _playlist_write_result(response: object) -> dict:
    # The payload of a write RPC that reports a missing playlist as a domain
    # answer: the playlist can be deleted between the permission check and
    # the call, so the RPC not finding it is the same 404 that check raises,
    # not an anomaly. Any other refusal is one.
    #
    # add_playlist_track is deliberately not one of these: it answers the
    # missing playlist the same way, but it also refuses the duplicate and
    # returns a position rather than a payload. SEE: _added_position
    data = _rpc_payload(response)

    if data.get("ok"):
        return data

    if data.get("error") == "playlist_not_found":
        raise NotFound("playlist_not_found")

    raise UpstreamError()


def _added_position(response: object) -> int:
    # The 1-based index the RPC computed for the new row, under its own
    # lock, right after the insert -- not a value read from a stored
    # column (#137) -- or the domain exception the refusal means. The
    # payload's id is deliberately dropped: it is the playlist_tracks row
    # id, while PlaylistTrack.id is the catalog uuid the caller already
    # gets back from the metadata upsert.
    data = _rpc_payload(response)

    if data.get("ok"):
        # A payload without a position is an upstream anomaly, and the
        # KeyError is already translated into one.
        return data["position"]

    # The playlist can be deleted between the permission check and the call,
    # so the RPC not finding it is the same 404 that check raises, not an
    # anomaly.
    if data.get("error") == "playlist_not_found":
        raise NotFound("playlist_not_found")

    if data.get("error") == "track_already_in_playlist":
        raise Conflict("track_already_in_playlist")

    raise UpstreamError()


def _last_order_key(db: Client, playlist_id: str) -> str | None:
    # The highest order_key in the playlist, or None for an empty one --
    # that is a real answer ("no last row"), not an error swallowed into a
    # made-up value. A failure here is a database failure like any other
    # and is left to the translated block that wraps every caller.
    response = (
        db.table("playlist_tracks")
        .select("order_key")
        .eq("playlist_id", playlist_id)
        .order("order_key", desc=True)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]["order_key"]


def _add_playlist_track(
    db: Client, user_id: str, playlist_id: str, track_uuid: str, order_key: str
) -> object:
    # Links one track and computes its position in a single statement, so
    # two concurrent adds can neither duplicate a track nor collide on
    # order_key. Like the other RPCs here it takes the caller explicitly
    # rather than reading auth.uid(): the RPC is not redefined by #143, and
    # an authenticated route now runs it on the user-scoped client, where
    # auth.uid() is the caller, not null -- the explicit p_added_by is kept
    # so the RPC's own logic does not depend on which client calls it. The
    # order_key is computed by the caller; position is still computed by
    # the RPC, from the row count under its lock, not read from a stored
    # column (#137).
    return db.rpc(
        "add_playlist_track",
        {
            "p_playlist_id": playlist_id,
            "p_track_id": track_uuid,
            "p_added_by": user_id,
            "p_order_key": order_key,
        },
    ).execute()


def _added_and_skipped(response: object) -> tuple[int, int]:
    # What add_playlist_tracks_bulk did with the batch. Its skipped is
    # counted against the array it was sent, which is already deduplicated,
    # so it covers the tracks that were already in the playlist and nothing
    # else.
    data = _playlist_write_result(response)

    # A payload without both counts is an upstream anomaly, and the KeyError
    # is already translated into one.
    return data["added"], data["skipped"]


def _add_playlist_tracks_bulk(
    db: Client,
    user_id: str,
    playlist_id: str,
    track_uuids: list[str],
    order_keys: list[str],
) -> object:
    # Links the whole batch in one statement: the RPC locks the playlist and
    # skips the tracks already in it, so a batch either lands whole or not
    # at all. Takes the caller explicitly for the same reason as the other
    # RPCs here. The keys are computed by the caller, one per track_uuids in
    # the same order. This RPC's result carries no position (#137): a
    # batch's tracks get theirs the same way any other track does, from the
    # index GET /playlists/{id}/tracks computes on read.
    return db.rpc(
        "add_playlist_tracks_bulk",
        {
            "p_playlist_id": playlist_id,
            "p_track_ids": track_uuids,
            "p_added_by": user_id,
            "p_order_keys": order_keys,
        },
    ).execute()


def _removed_count(response: object) -> int:
    # How many links remove_playlist_track deleted. Zero is a normal answer,
    # not a refusal: the track was not in the playlist, or the catalog has
    # never heard of it.
    data = _playlist_write_result(response)

    # A payload without the count is an upstream anomaly, and the KeyError
    # is already translated into one.
    return data["deleted"]


def _remove_playlist_track(db: Client, playlist_id: str, track_id: str) -> object:
    # Unlinks one track under the playlist row lock, so it takes the same
    # lock order as the writers that add. Unlike them it takes the provider
    # id: it resolves the catalog uuid itself, which is why this path has no
    # lookup of its own. It also does not take the caller — nothing is
    # written that records who removed the track.
    return db.rpc(
        "remove_playlist_track",
        {"p_playlist_id": playlist_id, "p_track_id": track_id},
    ).execute()


def _duration_total(response: object) -> int:
    # get_playlist_duration_total returns the bare bigint, which supabase-py
    # hands back as a scalar, a one-element list or a one-key dict depending
    # on how PostgREST shapes a scalar-returning function's response -- the
    # same ambiguity _rpc_payload and _rpc_playlist_ids already handle. A
    # payload that is not an int (including a missing/null one) is an
    # upstream anomaly, not a 0: the 0 of an empty playlist is produced by
    # the RPC's own COALESCE, never invented here. bool is checked
    # separately because in Python bool is a subclass of int, so a stray
    # `true` would otherwise pass as 1.
    data = getattr(response, "data", None)

    if isinstance(data, list):
        data = data[0] if data else None

    if isinstance(data, dict):
        data = next(iter(data.values()), None)

    if isinstance(data, bool) or not isinstance(data, int):
        raise UpstreamError()

    return data


def _get_playlist_duration_total(db: Client, playlist_id: str) -> int:
    with translate_upstream_errors():
        response = db.rpc(
            "get_playlist_duration_total",
            {"p_playlist_id": playlist_id},
        ).execute()

        return _duration_total(response)


def _rpc_playlist_ids(response: object) -> list[str]:
    # get_owned_playlists_with_track returns the ids themselves rather than
    # an ok/error envelope. Postgres set-returning functions come back as a
    # list, of bare ids for a scalar return type and of single-key rows for
    # a table one; a jsonb return arrives as an object instead.
    data = getattr(response, "data", None)

    if data is None:
        return []

    if isinstance(data, dict):
        data = data.get("playlist_ids") or []

    if not isinstance(data, list):
        raise UpstreamError()

    # A table return gives one column per row, whose name is the RPC's
    # business, not this service's. Take the value rather than guess the key.
    return [next(iter(row.values())) if isinstance(row, dict) else row for row in data]


def create_playlist(
    db: Client, owner_id: str, payload: CreatePlaylistRequest
) -> Playlist:
    with translate_upstream_errors():
        # Spread first so an owner_id sent in the body is overwritten.
        insert_payload = {**payload.model_dump(), "owner_id": owner_id}

        response = db.table("playlists").insert(insert_payload).execute()

        # An insert returning no row is an upstream anomaly. Indexing
        # blindly would raise IndexError, which is not translated, and
        # surface as a 500.
        if not response.data:
            raise UpstreamError()

        return Playlist(**response.data[0])


def list_playlists(
    db: Client, user_id: str, page: PageRequest
) -> tuple[list[Playlist], PageBlock]:
    with translate_upstream_errors():
        # Decoded here, ahead of any db.table() call, so a bad cursor never
        # reaches the database — apply_page decodes it again below to build
        # the filter, but by then it is already known to be valid.
        page.decode(_LIST_SORT)

        # Scoped on owner_id rather than can_edit on purpose: can_edit
        # answers "may this user modify this row", this query answers
        # "which rows are theirs". Collaborators will widen this query.
        query = (
            db.table("playlists")
            .select(_COLUMNS, count=page.count_mode)
            .eq("owner_id", user_id)
        )
        query = apply_page(query, _LIST_SORT, page)

        response = query.execute()

        rows, block = build_page(response.data or [], page, _LIST_SORT, response.count)
        return [Playlist(**row) for row in rows], block


def _count_playlist_tracks(db: Client, playlist_id: str) -> int:
    with translate_upstream_errors():
        response = (
            db.table("playlist_tracks")
            .select("id", count="exact")
            .eq("playlist_id", playlist_id)
            .limit(1)
            .execute()
        )

        return response.count or 0


def get_playlist(db: Client, user_id: str, playlist_id: str) -> PlaylistDetail:
    playlist = _get_editable_playlist(db, user_id, playlist_id)
    total_count = _count_playlist_tracks(db, playlist_id)
    total_duration_seconds = _get_playlist_duration_total(db, playlist_id)

    return PlaylistDetail(
        **playlist.model_dump(),
        total_count=total_count,
        total_duration_seconds=total_duration_seconds,
    )


# The one function this whole domain's public-share safety rests on. It is
# deliberately NOT a reuse of _get_editable_playlist: that one filters by
# id and then checks can_edit(user_id, playlist), and this endpoint has no
# user_id at all -- these are the public /public/playlists/{id} routes,
# served with no token. The .eq("is_public", True) below is explicit
# because these public routes run on the service-role client (see the
# "Supabase schema facts" memory note), so visibility is enforced by this
# filter rather than by Supabase RLS; unlike the six user-data domains,
# #143 does not change the client this function runs on. A playlist
# that does not exist and one that exists but is not public raise the same
# NotFound("playlist_not_found"), on purpose: the response must never
# confirm that someone else's private playlist exists. is_public is
# nullable with a default of false (017:1341); a stored null therefore
# never matches .eq(..., True), which is what makes the filter correct.
def get_public_playlist(db: Client, playlist_id: str) -> PlaylistWithTracks:
    with translate_upstream_errors():
        response = (
            db.table("playlists")
            .select(_COLUMNS)
            .eq("id", playlist_id)
            .eq("is_public", True)
            .execute()
        )

        if not response.data:
            raise NotFound("playlist_not_found")

        playlist = Playlist(**response.data[0])

    # Deliberately outside the block above, mirroring get_playlist(): these
    # two calls do not depend on user_id or on any permission check, so
    # they are reused as-is.
    tracks, total_count = _list_playlist_tracks(db, playlist_id)
    total_duration_seconds = _get_playlist_duration_total(db, playlist_id)

    return PlaylistWithTracks(
        **playlist.model_dump(),
        tracks=tracks,
        total_count=total_count,
        has_more=total_count > len(tracks),
        total_duration_seconds=total_duration_seconds,
    )


# The RPC of the USER domain: it reads playlist_tracks, joined to
# tracks.id (the catalog uuid). services/genre_service.py has its own,
# get_genre_playlist_thumbnails, which calls a different RPC
# (get_playlist_thumbnails) over genre_playlist_tracks, joined to
# tracks.track_id. The two names are close on purpose (006_genre.sql) and
# crossing them does not raise -- it silently returns [] because the RPC
# looks for the id in the wrong table. This function must never call
# "get_playlist_thumbnails", and services/genre_service.py must never call
# "get_user_playlist_thumbnails".
def get_user_playlist_thumbnails(db: Client, playlist_id: str) -> list[str]:
    with translate_upstream_errors():
        response = db.rpc(
            "get_user_playlist_thumbnails",
            {
                "playlist_ids": [playlist_id],
                "limit_per_playlist": _THUMBNAILS_PER_PLAYLIST,
            },
        ).execute()

        # A row's key is named, not taken positionally like
        # _rpc_playlist_ids does: get_user_playlist_thumbnails returns a
        # TABLE(playlist_id, thumbnail_url), two named columns, not one
        # whose name is this RPC's own business. A row without
        # thumbnail_url is an upstream anomaly and the resulting KeyError
        # is already translated into a 502 by the block above.
        #
        # An empty list here is a normal result, not a swallowed error:
        # the RPC itself filters out NULL and '' thumbnail_url rows, so a
        # playlist with no tracks, or whose first tracks have none, comes
        # back as [] on purpose. Called unconditionally, even for an empty
        # playlist, the same way get_playlist() calls the duration RPC
        # unconditionally.
        return [row["thumbnail_url"] for row in response.data or []]


def _liked_summary(db: Client, user_id: str) -> tuple[int, str | None]:
    with translate_upstream_errors():
        # One lightweight query for both figures get_liked_playlist() needs
        # up front: total_count (the exact count) and the oldest active
        # like's created_at (the first row in ascending order) -- no track
        # rows read here at all, unlike the old _list_liked_tracks (#139).
        response = (
            db.table("user_likes")
            .select("created_at", count="exact")
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .order("created_at")
            .limit(1)
            .execute()
        )

        total_count = response.count or 0
        oldest_created_at = response.data[0]["created_at"] if response.data else None

        return total_count, oldest_created_at


def _latest_liked_created_at(db: Client, user_id: str) -> str | None:
    with translate_upstream_errors():
        # Not derived from _liked_summary()'s read above: that query orders
        # ascending for the oldest like, so the most recent one needs its
        # own query, descending.
        response = (
            db.table("user_likes")
            .select("created_at")
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if not response.data:
            return None

        return response.data[0]["created_at"]


def _get_liked_duration_total(db: Client, user_id: str) -> int:
    with translate_upstream_errors():
        response = db.rpc(
            "get_liked_tracks_duration_total",
            {"p_user_id": user_id},
        ).execute()

        return _duration_total(response)


def get_liked_playlist(db: Client, user_id: str) -> PlaylistDetail:
    total_count, oldest_created_at = _liked_summary(db, user_id)

    # The query below is skipped when the summary came back empty: that
    # already means there are no active likes, so there is no "most recent
    # like" to look up.
    latest_created_at = (
        _latest_liked_created_at(db, user_id) if oldest_created_at is not None else None
    )

    # Called unconditionally, even with zero likes: the 0 is produced by
    # the RPC's own COALESCE, never invented here -- the same decision
    # get_playlist() makes for an empty real playlist.
    total_duration_seconds = _get_liked_duration_total(db, user_id)

    # A user with no active likes has no like to derive a timestamp from,
    # so both fall back to the request's own time.
    now = datetime.now(UTC).isoformat()

    return PlaylistDetail(
        id=_LIKED_PLAYLIST_ID,
        owner_id=user_id,
        title=_LIKED_PLAYLIST_ID,
        description=None,
        is_public=False,
        created_at=oldest_created_at or now,
        updated_at=latest_created_at or now,
        total_count=total_count,
        total_duration_seconds=total_duration_seconds,
    )


def update_playlist(
    db: Client, user_id: str, playlist_id: str, payload: UpdatePlaylistRequest
) -> Playlist:
    playlist = _get_editable_playlist(db, user_id, playlist_id)

    fields = payload.model_dump(exclude_unset=True)

    if not fields:
        return playlist

    with translate_upstream_errors():
        response = (
            db.table("playlists")
            .update(fields)
            .eq("id", playlist_id)
            .select(_COLUMNS)
            .execute()
        )

        # The row can be deleted between the read above and this write.
        # Indexing blindly here raised IndexError and returned a 500.
        if not response.data:
            raise NotFound("playlist_not_found")

        return Playlist(**response.data[0])


def delete_playlist(db: Client, user_id: str, playlist_id: str) -> None:
    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        response = db.table("playlists").delete().eq("id", playlist_id).execute()

        if not response.data:
            raise NotFound("playlist_not_found")


def add_track(
    db: Client,
    catalog_db: Client,
    user_id: str,
    playlist_id: str,
    item: AddPlaylistTrackRequest,
) -> PlaylistTrack:
    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        # The catalog write stays: the RPC links a catalog uuid, and only the
        # upsert can produce one for a track the catalog has not seen.
        track_uuid = _upsert_tracks(catalog_db, [item])[item.track_id]

        for _ in range(_ORDER_KEY_ATTEMPTS):
            order_key = generate_key_between(_last_order_key(db, playlist_id), None)
            response = _add_playlist_track(
                db, user_id, playlist_id, track_uuid, order_key
            )

            if not _is_order_key_conflict(response):
                # Deliberately a domain exception raised inside the block:
                # Conflict is not in the translated set, so it passes
                # through as a 409.
                position = _added_position(response)
                return PlaylistTrack(
                    **item.model_dump(), id=track_uuid, position=position
                )

        # Every attempt collided with another write to the same gap: each
        # RPC call rolled back its own insert on the way, so nothing was
        # written, and the caller can simply retry the whole request.
        raise Conflict("order_key_conflict")


def add_tracks(
    db: Client,
    catalog_db: Client,
    user_id: str,
    playlist_id: str,
    payload: BulkAddPlaylistTracksRequest,
) -> BulkAddResult:
    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        # First occurrence wins, so the tracks are added in the order the
        # batch was sent in. A track repeated in one batch is added once and
        # counted as skipped for the rest.
        #
        # Deduplicating here is not only about the counts: the catalog upsert
        # below is one ON CONFLICT statement, which Postgres refuses if the
        # same row is touched twice.
        unique_items: dict[str, AddPlaylistTrackRequest] = {}
        for item in payload.tracks:
            unique_items.setdefault(item.track_id, item)

        # The catalog write stays in Python: the RPC links catalog uuids, and
        # only the upsert can produce one for a track the catalog has not
        # seen. Its result is the provider id -> uuid mapping the call below
        # is built from.
        uuid_by_track_id = _upsert_tracks(catalog_db, list(unique_items.values()))

        # A provider id missing from the upsert result cannot happen: it is
        # the key the rows were written on. If it ever did, the KeyError
        # becomes a 502.
        track_uuids = [uuid_by_track_id[track_id] for track_id in unique_items]

        for _ in range(_ORDER_KEY_ATTEMPTS):
            # One key per unique track, in the same order as track_uuids.
            # A key is generated even for a track the RPC is about to skip
            # because it is already in the playlist -- that key is simply
            # never written, and a hole in the key space breaks nothing.
            order_keys = generate_n_keys_between(
                _last_order_key(db, playlist_id), None, len(track_uuids)
            )
            response = _add_playlist_tracks_bulk(
                db, user_id, playlist_id, track_uuids, order_keys
            )

            if not _is_order_key_conflict(response):
                added, skipped = _added_and_skipped(response)

                # The RPC never sees a repeat, so what it skipped covers only
                # the tracks already in the playlist. The repeats removed
                # above are the rest, which keeps added + skipped equal to
                # the batch as sent.
                skipped += len(payload.tracks) - len(unique_items)

                return BulkAddResult(added=added, skipped=skipped)

        # Every attempt collided with another write to the same gap: each
        # RPC call rolled back its own insert on the way, so nothing was
        # written, and the caller can simply retry the whole request.
        raise Conflict("order_key_conflict")


def remove_track(db: Client, user_id: str, playlist_id: str, track_id: str) -> None:
    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        response = _remove_playlist_track(db, playlist_id, track_id)

        # The count is checked but not reported: removing is idempotent, so
        # a track that was not there and one that was are the same answer to
        # the caller. A track the catalog has never heard of is the same
        # again — certainly not in the playlist, which is the state that was
        # asked for.
        _removed_count(response)


def _move_order_key(
    db: Client, playlist_id: str, old_position: int, new_position: int
) -> str:
    # 1-based positions become 0-based indices: o is the moved row's
    # current index, t is its destination. The window read below is the
    # smallest slice of at most 3 rows, ordered by order_key, guaranteed to
    # contain both of the destination's real neighbours, whichever way the
    # row moves. Example, three rows with keys a0, a1, a2: moving 1-based
    # position 1 to 3 is o=0, t=2; the window is range(1, 3) = [a1, a2],
    # and since t > o the neighbours are before=at(2)=a2, after=at(3)=
    # None (past the end), so the new key lands after a2.
    o = old_position - 1
    t = new_position - 1

    start = max(t - 1, 0)
    response = (
        db.table("playlist_tracks")
        .select("order_key")
        .eq("playlist_id", playlist_id)
        .order("order_key")
        .range(start, t + 1)
        .execute()
    )
    rows = response.data or []

    def at(index: int) -> str | None:
        # The key at absolute index `index`, or None if it falls outside
        # the window read above (including a negative index, which no
        # window ever covers).
        if index < 0:
            return None
        offset = index - start
        if not (0 <= offset < len(rows)):
            return None
        return rows[offset]["order_key"]

    if t < o:
        before, after = at(t - 1), at(t)
    elif t > o:
        before, after = at(t), at(t + 1)
    else:
        # Noop: the RPC returns without writing order_key, but this branch
        # stays uniform with the other two rather than special-cased away.
        before, after = at(t - 1), at(t + 1)

    return generate_key_between(before, after)


def move_track(
    db: Client, user_id: str, playlist_id: str, payload: MovePlaylistTrackRequest
) -> None:
    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        response = (
            db.table("playlist_tracks")
            .select("track_id", count="exact")
            .eq("playlist_id", playlist_id)
            .limit(1)
            .execute()
        )

        track_count = response.count or 0

    # Outside the block above: rejecting a position is a decision about the
    # request, not a database failure, the same reasoning as the can_edit
    # check in _get_editable_playlist. The RPC clamps an out-of-range index
    # to the nearest valid one, so passing one straight through would report
    # a move that silently did something else.
    if not _is_valid_position(payload.old_position, track_count):
        raise InvalidRequest()

    if not _is_valid_position(payload.new_position, track_count):
        raise InvalidRequest()

    with translate_upstream_errors():
        for _ in range(_ORDER_KEY_ATTEMPTS):
            order_key = _move_order_key(
                db, playlist_id, payload.old_position, payload.new_position
            )
            response = db.rpc(
                "move_playlist_track",
                {
                    "p_playlist_id": playlist_id,
                    "p_old_index": payload.old_position,
                    "p_new_index": payload.new_position,
                    "p_order_key": order_key,
                },
            ).execute()

            # Checked before _rpc_result: that helper turns any ok: false
            # into a 502, which would swallow the one refusal this loop
            # needs to see and retry on.
            if not _is_order_key_conflict(response):
                _rpc_result(response)
                return

        # Every attempt collided with another write to the same gap: each
        # RPC call rolled back its own write on the way, so the caller can
        # simply retry the whole request.
        raise Conflict("order_key_conflict")


def list_owned_playlists_with_track(
    db: Client, user_id: str, track_id: str
) -> OwnedPlaylistIds:
    with translate_upstream_errors():
        # The RPC takes the caller explicitly rather than reading auth.uid():
        # it is not redefined by #143, and GET /playlists/owned-with-track/
        # {track_id} is one of the authenticated routes that now runs on the
        # user-scoped client, where auth.uid() is the caller, not null -- the
        # explicit p_user_id keeps the RPC's own logic independent of which
        # client calls it. It joins to tracks on the provider id itself, so
        # there is nothing to resolve first.
        response = db.rpc(
            "get_owned_playlists_with_track",
            {"p_user_id": user_id, "p_track_id": track_id},
        ).execute()

        # A track in none of the caller's playlists is a real answer to a
        # membership question, not an empty state: ok:true with an empty
        # list, like an empty GET /likes/sync window.
        return OwnedPlaylistIds(playlist_ids=_rpc_playlist_ids(response))
