# INFO: Reads and writes the authenticated user's playlists in Supabase.

from supabase import Client

from core.exceptions import NotFound, ResourceEmpty, UpstreamError
from core.upstream import translate_upstream_errors
from models.playlists import (
    CreatePlaylistRequest,
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
