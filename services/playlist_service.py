# INFO: Reads and writes the authenticated user's playlists in Supabase.

from supabase import Client

from core.exceptions import (
    Conflict,
    InvalidRequest,
    NotFound,
    ResourceEmpty,
    UpstreamError,
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
    PlaylistList,
    PlaylistTrack,
    UpdatePlaylistRequest,
)

_COLUMNS = "id, owner_id, title, description, is_public, created_at, updated_at"

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


def _next_position(db: Client, playlist_id: str) -> int:
    response = (
        db.table("playlist_tracks")
        .select("position")
        .eq("playlist_id", playlist_id)
        .order("position", desc=True)
        .limit(1)
        .execute()
    )

    # The highest position plus one, not the track count plus one: removing
    # a track leaves its position free, and ux_playlist_pos on
    # (playlist_id, position) would reject a reused one. Positions start at
    # 1, matching the move_playlist_track RPC's indices.
    if not response.data:
        return 1

    return response.data[0]["position"] + 1


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


def _linked_track_ids(db: Client, playlist_id: str, track_uuids: list[str]) -> set[str]:
    # playlist_tracks has no unique on (playlist_id, track_id) — its only
    # unique is ux_playlist_pos — so a duplicate cannot be caught as a 23505
    # the way username_taken is in profile_service. It takes a read.
    linked: set[str] = set()

    # Same URI-size guard as the catalog read above: in_ goes into the query
    # string, and a full batch of uuids in one filter builds a URI Supabase
    # rejects.
    for start in range(0, len(track_uuids), _TRACK_BATCH_SIZE):
        batch = track_uuids[start : start + _TRACK_BATCH_SIZE]
        response = (
            db.table("playlist_tracks")
            .select("track_id")
            .eq("playlist_id", playlist_id)
            .in_("track_id", batch)
            .execute()
        )
        linked.update(row["track_id"] for row in response.data)

    return linked


def _resolve_track_uuid(db: Client, track_id: str) -> str | None:
    # The track endpoints address a track by its provider id, the one a
    # client holds during playback. playlist_tracks joins on the catalog
    # uuid, so the two have to be bridged here.
    response = db.table("tracks").select("id").eq("track_id", track_id).execute()

    if not response.data:
        return None

    return response.data[0]["id"]


def _is_valid_position(position: int, track_count: int) -> bool:
    return 1 <= position <= track_count


def _rpc_result(response: object) -> dict:
    # move_playlist_track reports its own failures in the payload rather than
    # as an error status — {"ok": true, "order": [...]} or {"ok": false,
    # "error": ...} — so a successful round trip can still mean the function
    # refused. supabase-py hands the returned json back as-is, which is a
    # bare object for a scalar return and a one-element list for a set one.
    data = getattr(response, "data", None)

    if isinstance(data, list):
        data = data[0] if data else None

    if not isinstance(data, dict) or not data.get("ok"):
        raise UpstreamError()

    return data


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


def list_playlists(db: Client, user_id: str) -> PlaylistList:
    with translate_upstream_errors():
        # Scoped on owner_id rather than can_edit on purpose: can_edit
        # answers "may this user modify this row", this query answers
        # "which rows are theirs". Collaborators will widen this query.
        response = (
            db.table("playlists")
            .select(_COLUMNS)
            .eq("owner_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )

        if not response.data:
            raise ResourceEmpty("no_playlists")

        return PlaylistList(playlists=[Playlist(**row) for row in response.data])


def get_playlist(db: Client, user_id: str, playlist_id: str) -> PlaylistDetail:
    playlist = _get_editable_playlist(db, user_id, playlist_id)
    tracks, total_count = _list_playlist_tracks(db, playlist_id)

    return PlaylistDetail(
        **playlist.model_dump(),
        tracks=tracks,
        total_count=total_count,
        has_more=total_count > len(tracks),
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
        track_uuid = _upsert_tracks(db, [item])[item.track_id]

        # Deliberately a domain exception raised inside the block: Conflict
        # is not in the translated set, so it passes through as a 409.
        if _linked_track_ids(db, playlist_id, [track_uuid]):
            raise Conflict("track_already_in_playlist")

        position = _next_position(db, playlist_id)

        response = (
            db.table("playlist_tracks")
            .insert(
                {
                    "playlist_id": playlist_id,
                    "track_id": track_uuid,
                    "position": position,
                }
            )
            .execute()
        )

        # An insert returning no row is an upstream anomaly, as in
        # create_playlist.
        if not response.data:
            raise UpstreamError()

        return PlaylistTrack(**item.model_dump(), id=track_uuid, position=position)


def add_tracks(
    db: Client,
    user_id: str,
    playlist_id: str,
    payload: BulkAddPlaylistTracksRequest,
) -> BulkAddResult:
    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        # First occurrence wins, so the positions written below follow the
        # order the batch was sent in. A track repeated in one batch is
        # added once and counted as skipped for the rest.
        unique_items: dict[str, AddPlaylistTrackRequest] = {}
        for item in payload.tracks:
            unique_items.setdefault(item.track_id, item)

        uuid_by_track_id = _upsert_tracks(db, list(unique_items.values()))

        # A provider id missing from the upsert result cannot happen: it is
        # the key the rows were written on. If it ever did, the KeyError
        # becomes a 502.
        ordered_uuids = [uuid_by_track_id[track_id] for track_id in unique_items]

        linked = _linked_track_ids(db, playlist_id, ordered_uuids)
        to_add = [uuid for uuid in ordered_uuids if uuid not in linked]

        # Everything the caller sent that did not become a new entry, whether
        # it was a duplicate inside the batch or already in the playlist.
        skipped = len(payload.tracks) - len(to_add)

        if not to_add:
            return BulkAddResult(added=0, skipped=skipped)

        start_position = _next_position(db, playlist_id)

        db.table("playlist_tracks").insert(
            [
                {
                    "playlist_id": playlist_id,
                    "track_id": track_uuid,
                    "position": start_position + offset,
                }
                for offset, track_uuid in enumerate(to_add)
            ]
        ).execute()

        return BulkAddResult(added=len(to_add), skipped=skipped)


def remove_track(db: Client, user_id: str, playlist_id: str, track_id: str) -> None:
    _get_editable_playlist(db, user_id, playlist_id)

    with translate_upstream_errors():
        track_uuid = _resolve_track_uuid(db, track_id)

        # A track the catalog has never heard of is not in the playlist,
        # which is the state the caller asked for. Removing is idempotent,
        # so this is a success, not a 404.
        if track_uuid is None:
            return

        # The delete result is not inspected for the same reason: removing a
        # track that is not there changes nothing and is still a success.
        (
            db.table("playlist_tracks")
            .delete()
            .eq("playlist_id", playlist_id)
            .eq("track_id", track_uuid)
            .execute()
        )


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
