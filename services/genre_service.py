# INFO: Reads genres from Supabase.

from supabase import Client

from core.exceptions import NotFound, ResourceEmpty, UpstreamError
from core.upstream import translate_upstream_errors
from models.genres import (
    Genre,
    GenreCategoryList,
    GenreList,
    GenrePlaylist,
    GenrePlaylistList,
    GenrePlaylistTrack,
    GenrePlaylistTrackList,
)


def _get_genre_id(db: Client, slug: str) -> str:
    with translate_upstream_errors():
        genre_response = db.table("genres").select("id").eq("slug", slug).execute()

        if not genre_response.data:
            raise NotFound("genre_not_found")

        return genre_response.data[0]["id"]


def list_genres(db: Client) -> GenreList:
    with translate_upstream_errors():
        response = (
            db.table("genres")
            .select("slug, name, description")
            .order("sort_order")
            .execute()
        )

        if not response.data:
            raise ResourceEmpty("no_genres")

        return GenreList(genres=[Genre(**row) for row in response.data])


def get_genre_playlists(db: Client, slug: str) -> GenrePlaylistList:
    genre_id = _get_genre_id(db, slug)

    with translate_upstream_errors():
        playlists_response = (
            db.table("genre_playlists")
            .select("id, title, description, thumbnail_url, track_count, category")
            .eq("genre_id", genre_id)
            .order("sort_order")
            .execute()
        )

        if not playlists_response.data:
            raise ResourceEmpty("no_playlists")

        return GenrePlaylistList(
            playlists=[GenrePlaylist(**row) for row in playlists_response.data]
        )


def get_genre_categories(db: Client, slug: str) -> GenreCategoryList:
    genre_id = _get_genre_id(db, slug)

    with translate_upstream_errors():
        playlists_response = (
            db.table("genre_playlists")
            .select("category")
            .eq("genre_id", genre_id)
            .execute()
        )

        categories = {
            row["category"]
            for row in playlists_response.data
            if row["category"] is not None
        }

        if not categories:
            raise ResourceEmpty("no_categories")

        return GenreCategoryList(categories=sorted(categories))


def get_genre_playlist_tracks(db: Client, playlist_id: str) -> GenrePlaylistTrackList:
    with translate_upstream_errors():
        playlist_response = (
            db.table("genre_playlists").select("id").eq("id", playlist_id).execute()
        )

        if not playlist_response.data:
            raise NotFound("playlist_not_found")

    with translate_upstream_errors():
        playlist_tracks_response = (
            db.table("genre_playlist_tracks")
            .select("track_id, position")
            .eq("playlist_id", playlist_id)
            .order("position")
            .limit(500)
            .execute()
        )

        if not playlist_tracks_response.data:
            raise ResourceEmpty("no_tracks")

        ordered_ids = [row["track_id"] for row in playlist_tracks_response.data]
        positions = [row["position"] for row in playlist_tracks_response.data]

        tracks_response = (
            db.table("tracks")
            .select(
                "track_id, title, artists, album, album_id, "
                "duration_seconds, thumbnail_url"
            )
            .in_("track_id", ordered_ids)
            .execute()
        )

        tracks_by_id = {row["track_id"]: row for row in tracks_response.data}

        # A track_id present in genre_playlist_tracks but absent from tracks
        # is malformed upstream data, not a Python exception to translate.
        if any(track_id not in tracks_by_id for track_id in ordered_ids):
            raise UpstreamError()

        tracks = [
            GenrePlaylistTrack(**tracks_by_id[track_id], position=position)
            for track_id, position in zip(ordered_ids, positions, strict=True)
        ]

    return GenrePlaylistTrackList(tracks=tracks)
