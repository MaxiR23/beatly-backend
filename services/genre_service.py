# INFO: Reads genres from Supabase.

from supabase import Client

from core.exceptions import NotFound, UpstreamError
from core.pagination import PageRequest, SortKey, ValueType, build_page
from core.thumbnails import square_thumbnail_url
from core.upstream import translate_upstream_errors
from models.genres import (
    Genre,
    GenrePlaylist,
    GenrePlaylistListItem,
    GenrePlaylistTrack,
)
from models.responses import PageBlock

# Declares the ORDER BY in one place. build_page does not require the id:
# it reads one only to emit a cursor, and these endpoints never emit one.
# The tiebreaker is here so that rows sharing a sort_order/position value
# come back in a stable order instead of an arbitrary one.
_GENRES_SORT = SortKey(
    "sort_order",
    ValueType.INT,
    descending=False,
    id_column="id",
    id_type=ValueType.UUID,
)
_GENRE_PLAYLISTS_SORT = SortKey(
    "sort_order",
    ValueType.INT,
    descending=False,
    id_column="id",
    id_type=ValueType.UUID,
)
_PLAYLIST_TRACKS_SORT = SortKey(
    "position", ValueType.INT, descending=False, id_column="id", id_type=ValueType.UUID
)

# Shared by get_genre_playlists() and get_genre_playlist(): the same
# columns of the same table, kept in one place so the two selects cannot
# drift apart.
_GENRE_PLAYLIST_COLUMNS = "id, title, description, thumbnail_url, track_count, category"

# Cap of the mosaic of the public share and of GET /genres/{slug}/playlists,
# not this RPC's default: see the matching constant and comment in
# services/playlist_service.py.
_THUMBNAILS_PER_PLAYLIST = 4

# Side of the square image the stored thumbnail_url is rewritten to when it
# is read, with smart crop. It is never rewritten when written: what is
# stored stays as the client sent it, so old and new rows are both covered.
_THUMBNAIL_SIZE = 544


def _whole_collection(rows: list[dict]) -> PageRequest:
    # The limit of this page is the collection itself, not something the
    # client asked for: with it, build_page derives has_more False and
    # next_cursor None by construction, with no probe row to request and no
    # cursor to ever emit.
    return PageRequest(limit=len(rows))


def _get_genre_id(db: Client, slug: str) -> str:
    with translate_upstream_errors():
        genre_response = db.table("genres").select("id").eq("slug", slug).execute()

        if not genre_response.data:
            raise NotFound("genre_not_found")

        return genre_response.data[0]["id"]


def list_genres(db: Client) -> tuple[list[Genre], PageBlock]:
    with translate_upstream_errors():
        response = (
            db.table("genres")
            .select("id, slug, name, description")
            .order(_GENRES_SORT.column, desc=_GENRES_SORT.descending)
            .order(_GENRES_SORT.id_column, desc=_GENRES_SORT.descending)
            .execute()
        )

        rows = response.data or []
        page_rows, block = build_page(
            rows, _whole_collection(rows), _GENRES_SORT, len(rows)
        )
        return [Genre(**row) for row in page_rows], block


def get_genre_playlists(
    db: Client, slug: str
) -> tuple[list[GenrePlaylistListItem], PageBlock]:
    genre_id = _get_genre_id(db, slug)

    with translate_upstream_errors():
        playlists_response = (
            db.table("genre_playlists")
            .select(_GENRE_PLAYLIST_COLUMNS)
            .eq("genre_id", genre_id)
            .order(_GENRE_PLAYLISTS_SORT.column, desc=_GENRE_PLAYLISTS_SORT.descending)
            .order(
                _GENRE_PLAYLISTS_SORT.id_column, desc=_GENRE_PLAYLISTS_SORT.descending
            )
            .execute()
        )

        rows = playlists_response.data or []
        page_rows, block = build_page(
            rows, _whole_collection(rows), _GENRE_PLAYLISTS_SORT, len(rows)
        )
        mosaics = get_genre_playlists_thumbnails(db, [row["id"] for row in page_rows])
        items = [
            GenrePlaylistListItem(
                **_with_square_thumbnail(row), thumbnail_urls=mosaics.get(row["id"], [])
            )
            for row in page_rows
        ]
        return items, block


def get_genre_categories(db: Client, slug: str) -> tuple[list[str], PageBlock]:
    genre_id = _get_genre_id(db, slug)

    with translate_upstream_errors():
        playlists_response = (
            db.table("genre_playlists")
            .select("category")
            .eq("genre_id", genre_id)
            .execute()
        )

        categories = sorted(
            {
                row["category"]
                for row in playlists_response.data
                if row["category"] is not None
            }
        )

        # No SortKey here on purpose: categories are not rows with an order
        # column and an id, they are distinct genre_playlists.category
        # values, deduplicated and sorted in Python. A SortKey exists to
        # emit and decode cursors, and this endpoint never emits one, so
        # declaring one would be dead machinery that makes this look like
        # real pagination to a reader comparing it with the other three.
        return categories, PageBlock(
            limit=len(categories),
            next_cursor=None,
            has_more=False,
            total=len(categories),
        )


def get_genre_playlist_tracks(
    db: Client, playlist_id: str
) -> tuple[list[GenrePlaylistTrack], PageBlock]:
    with translate_upstream_errors():
        playlist_response = (
            db.table("genre_playlists").select("id").eq("id", playlist_id).execute()
        )

        if not playlist_response.data:
            raise NotFound("playlist_not_found")

    with translate_upstream_errors():
        playlist_tracks_response = (
            db.table("genre_playlist_tracks")
            .select("id, track_id, position")
            .eq("playlist_id", playlist_id)
            .order(_PLAYLIST_TRACKS_SORT.column, desc=_PLAYLIST_TRACKS_SORT.descending)
            .order(
                _PLAYLIST_TRACKS_SORT.id_column, desc=_PLAYLIST_TRACKS_SORT.descending
            )
            .execute()
        )

        playlist_track_rows = playlist_tracks_response.data or []

        page_rows, block = build_page(
            playlist_track_rows,
            _whole_collection(playlist_track_rows),
            _PLAYLIST_TRACKS_SORT,
            len(playlist_track_rows),
        )

        if not page_rows:
            # Skip the tracks query rather than hit the database with an
            # empty in_(): there is nothing to resolve.
            return [], block

        ordered_ids = [row["track_id"] for row in page_rows]
        positions = [row["position"] for row in page_rows]

        tracks_response = (
            db.table("tracks")
            .select(
                "track_id, title, artists, album, album_id, "
                "duration_seconds, thumbnail_url"
            )
            .in_("track_id", ordered_ids)
            .execute()
        )

        tracks_by_id = {
            row["track_id"]: _with_square_thumbnail(row) for row in tracks_response.data
        }

        # A track_id present in genre_playlist_tracks but absent from tracks
        # is malformed upstream data, not a Python exception to translate.
        if any(track_id not in tracks_by_id for track_id in ordered_ids):
            raise UpstreamError()

        tracks = [
            GenrePlaylistTrack(**tracks_by_id[track_id], position=position)
            for track_id, position in zip(ordered_ids, positions, strict=True)
        ]

        return tracks, block


# The one lookup that reads a single curated playlist's own metadata
# (title, description, thumbnail_url, category) by id. Neither
# get_genre_playlists() (filters by genre_id, returns many) nor
# get_genre_playlist_tracks() (reads only the tracks) does this today.
# Used by the public share endpoint; a request also pays for the
# existence check get_genre_playlist_tracks() does on its own (a second,
# identical lookup by primary key), which is accepted rather than
# threading a "skip the check" flag through the tracks path.
def get_genre_playlist(db: Client, playlist_id: str) -> GenrePlaylist:
    with translate_upstream_errors():
        response = (
            db.table("genre_playlists")
            .select(_GENRE_PLAYLIST_COLUMNS)
            .eq("id", playlist_id)
            .execute()
        )

        if not response.data:
            raise NotFound("playlist_not_found")

        return GenrePlaylist(**_with_square_thumbnail(response.data[0]))


# The RPC of the GENRE domain: it reads genre_playlist_tracks, joined to
# tracks.track_id (the provider id). services/playlist_service.py has its
# own, get_user_playlist_thumbnails, which calls a different RPC
# (get_user_playlist_thumbnails) over playlist_tracks, joined to
# tracks.id. The Python name below deliberately says "genre", even though
# the RPC it calls does not (006_genre.sql, db/migrations/README.md
# finding 2): the ambiguous name stays in the database, not in this
# module. This function must never call "get_user_playlist_thumbnails",
# and services/playlist_service.py must never call
# "get_playlist_thumbnails".
def get_genre_playlist_thumbnails(db: Client, playlist_id: str) -> list[str]:
    with translate_upstream_errors():
        response = db.rpc(
            "get_playlist_thumbnails",
            {
                "playlist_ids": [playlist_id],
                "limit_per_playlist": _THUMBNAILS_PER_PLAYLIST,
            },
        ).execute()

        # Same contract as get_user_playlist_thumbnails for the shape: a
        # row's key is named, an empty list is a normal result (no tracks,
        # or none of the first ones have a thumbnail), and a row without
        # thumbnail_url becomes a 502 through the KeyError the block above
        # already translates. As of 023_align_genre_playlist_thumbnail_filters.sql
        # the filter is the same as get_user_playlist_thumbnails's: both
        # drop a row whose thumbnail_url is NULL or ''. Since
        # public.tracks.thumbnail_url is NOT NULL, the '' half is the one
        # doing the work. So the list below can never contain ''.
        return [
            square_thumbnail_url(row["thumbnail_url"], _THUMBNAIL_SIZE, smart_crop=True)
            for row in response.data or []
        ]


# Batch version of get_genre_playlist_thumbnails, for GET
# /genres/{slug}/playlists: one RPC call per request, not one per playlist
# (#160). Same RPC, same rule: it must never call
# "get_user_playlist_thumbnails". Rows are grouped by playlist_id keeping the
# RPC's row order (the current body, 023, ends in ORDER BY playlist_id, rn).
# A playlist with no rows is absent from the dict; the caller uses
# .get(id, []). A malformed row becomes a 502 through the KeyError.
def get_genre_playlists_thumbnails(
    db: Client, playlist_ids: list[str]
) -> dict[str, list[str]]:
    if not playlist_ids:
        return {}

    with translate_upstream_errors():
        response = db.rpc(
            "get_playlist_thumbnails",
            {
                "playlist_ids": playlist_ids,
                "limit_per_playlist": _THUMBNAILS_PER_PLAYLIST,
            },
        ).execute()

        mosaics: dict[str, list[str]] = {}
        for row in response.data or []:
            mosaics.setdefault(row["playlist_id"], []).append(
                square_thumbnail_url(
                    row["thumbnail_url"], _THUMBNAIL_SIZE, smart_crop=True
                )
            )
        return mosaics


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
