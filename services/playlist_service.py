# INFO: Reads and writes the authenticated user's playlists in Supabase.

from supabase import Client

from core.exceptions import Conflict, InvalidRequest, NotFound, UpstreamError
from core.pagination import PageRequest, SortKey, ValueType, apply_page, build_page
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
    PlaylistTrack,
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

_TRACK_COLUMNS = (
    "id, track_id, title, artists, album, album_id, duration_seconds, thumbnail_url"
)

# An explicit cap, not pagination: the response reports total_count and
# has_more so a truncated playlist is never silently truncated.
_TRACKS_LIMIT = 1000

# in_ goes into the query string, so a full playlist's worth of uuids in
# one filter builds a URI Supabase rejects. Fetch them in batches.
_TRACK_BATCH_SIZE = 150


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


def _list_playlist_tracks(
    db: Client, playlist_id: str
) -> tuple[list[PlaylistTrack], int]:
    with translate_upstream_errors():
        entries_response = (
            db.table("playlist_tracks")
            .select("track_id, position", count="exact")
            .eq("playlist_id", playlist_id)
            .order("position")
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
        positions = [row["position"] for row in entries_response.data]

        # playlist_tracks.track_id is a uuid referencing tracks.id, not the
        # text tracks.track_id the curated genre path joins on.
        tracks_by_id = {}
        for start in range(0, len(ordered_ids), _TRACK_BATCH_SIZE):
            batch = ordered_ids[start : start + _TRACK_BATCH_SIZE]
            tracks_response = (
                db.table("tracks").select(_TRACK_COLUMNS).in_("id", batch).execute()
            )
            tracks_by_id.update({row["id"]: row for row in tracks_response.data})

        # A missing id here cannot happen: the foreign key cascades on
        # delete. If it ever did, the KeyError becomes a 502.
        tracks = [
            PlaylistTrack(**tracks_by_id[track_id], position=position)
            for track_id, position in zip(ordered_ids, positions, strict=True)
        ]

        return tracks, total_count


def _upsert_tracks(db: Client, items: list[AddPlaylistTrackRequest]) -> dict[str, str]:
    # tracks.track_id is unique (tracks_track_id_key), so a track already in
    # the catalog has its metadata refreshed rather than duplicated. Returns
    # provider id -> catalog uuid, the id playlist_tracks joins on.
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
    # The position add_playlist_track assigned, or the domain exception its
    # refusal means. The payload's id is deliberately dropped: it is the
    # playlist_tracks row id, while PlaylistTrack.id is the catalog uuid the
    # caller already gets back from the metadata upsert.
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


def _add_playlist_track(
    db: Client, user_id: str, playlist_id: str, track_uuid: str
) -> object:
    # Links one track and assigns its position in a single statement, so two
    # concurrent adds can neither duplicate a track nor collide on
    # ux_playlist_pos. Like the other RPCs here it takes the caller
    # explicitly: auth.uid() is null on the service-role client.
    return db.rpc(
        "add_playlist_track",
        {
            "p_playlist_id": playlist_id,
            "p_track_id": track_uuid,
            "p_added_by": user_id,
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
    db: Client, user_id: str, playlist_id: str, track_uuids: list[str]
) -> object:
    # Links the whole batch in one statement: the RPC locks the playlist,
    # skips the tracks already in it and assigns contiguous positions to the
    # rest, so a batch either lands whole or not at all. Takes the caller
    # explicitly for the same reason as the other RPCs here.
    return db.rpc(
        "add_playlist_tracks_bulk",
        {
            "p_playlist_id": playlist_id,
            "p_track_ids": track_uuids,
            "p_added_by": user_id,
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


def get_playlist(db: Client, user_id: str, playlist_id: str) -> PlaylistDetail:
    playlist = _get_editable_playlist(db, user_id, playlist_id)
    tracks, total_count = _list_playlist_tracks(db, playlist_id)
    total_duration_seconds = _get_playlist_duration_total(db, playlist_id)

    return PlaylistDetail(
        **playlist.model_dump(),
        tracks=tracks,
        total_count=total_count,
        has_more=total_count > len(tracks),
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
    db: Client, user_id: str, playlist_id: str, item: AddPlaylistTrackRequest
) -> PlaylistTrack:
    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        # The catalog write stays: the RPC links a catalog uuid, and only the
        # upsert can produce one for a track the catalog has not seen.
        track_uuid = _upsert_tracks(db, [item])[item.track_id]

        response = _add_playlist_track(db, user_id, playlist_id, track_uuid)

        # Deliberately a domain exception raised inside the block: Conflict
        # is not in the translated set, so it passes through as a 409.
        position = _added_position(response)

        return PlaylistTrack(**item.model_dump(), id=track_uuid, position=position)


def add_tracks(
    db: Client,
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
        uuid_by_track_id = _upsert_tracks(db, list(unique_items.values()))

        # A provider id missing from the upsert result cannot happen: it is
        # the key the rows were written on. If it ever did, the KeyError
        # becomes a 502.
        response = _add_playlist_tracks_bulk(
            db,
            user_id,
            playlist_id,
            [uuid_by_track_id[track_id] for track_id in unique_items],
        )

        added, skipped = _added_and_skipped(response)

        # The RPC never sees a repeat, so what it skipped covers only the
        # tracks already in the playlist. The repeats removed above are the
        # rest, which keeps added + skipped equal to the batch as sent.
        skipped += len(payload.tracks) - len(unique_items)

        return BulkAddResult(added=added, skipped=skipped)


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
        response = db.rpc(
            "move_playlist_track",
            {
                "p_playlist_id": playlist_id,
                "p_old_index": payload.old_position,
                "p_new_index": payload.new_position,
            },
        ).execute()

        _rpc_result(response)


def list_owned_playlists_with_track(
    db: Client, user_id: str, track_id: str
) -> OwnedPlaylistIds:
    with translate_upstream_errors():
        # The RPC takes the caller explicitly rather than reading auth.uid():
        # every query in this service runs on the service-role client, where
        # auth.uid() is null. It joins to tracks on the provider id itself,
        # so there is nothing to resolve first.
        response = db.rpc(
            "get_owned_playlists_with_track",
            {"p_user_id": user_id, "p_track_id": track_id},
        ).execute()

        # A track in none of the caller's playlists is a real answer to a
        # membership question, not an empty state: ok:true with an empty
        # list, like an empty GET /likes/sync window.
        return OwnedPlaylistIds(playlist_ids=_rpc_playlist_ids(response))
