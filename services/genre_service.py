# INFO: Reads genres from Supabase.

import httpx
from postgrest.exceptions import APIError
from pydantic import ValidationError
from supabase import Client

from core.exceptions import NotFound, ResourceEmpty, UpstreamError, UpstreamTimeout
from models.genres import (
    Genre,
    GenreCategoryList,
    GenreList,
    GenrePlaylist,
    GenrePlaylistList,
    GenrePlaylistTrack,
    GenrePlaylistTrackList,
)


def list_genres(db: Client) -> GenreList:
    try:
        response = (
            db.table("genres")
            .select("slug, name, description")
            .order("sort_order")
            .execute()
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (APIError, httpx.TransportError) as exc:
        raise UpstreamError() from exc

    if not response.data:
        raise ResourceEmpty("no_genres")

    try:
        return GenreList(genres=[Genre(**row) for row in response.data])
    except (ValidationError, TypeError) as exc:
        raise UpstreamError() from exc


def get_genre_playlists(db: Client, slug: str) -> GenrePlaylistList:
    try:
        genre_response = db.table("genres").select("id").eq("slug", slug).execute()
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (APIError, httpx.TransportError) as exc:
        raise UpstreamError() from exc

    if not genre_response.data:
        raise NotFound("genre_not_found")

    try:
        genre_id = genre_response.data[0]["id"]
    except (KeyError, TypeError, IndexError) as exc:
        raise UpstreamError() from exc

    try:
        playlists_response = (
            db.table("genre_playlists")
            .select("id, title, description, thumbnail_url, track_count, category")
            .eq("genre_id", genre_id)
            .order("sort_order")
            .execute()
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (APIError, httpx.TransportError) as exc:
        raise UpstreamError() from exc

    if not playlists_response.data:
        raise ResourceEmpty("no_playlists")

    try:
        return GenrePlaylistList(
            playlists=[GenrePlaylist(**row) for row in playlists_response.data]
        )
    except (ValidationError, TypeError) as exc:
        raise UpstreamError() from exc


def get_genre_categories(db: Client, slug: str) -> GenreCategoryList:
    try:
        genre_response = db.table("genres").select("id").eq("slug", slug).execute()
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (APIError, httpx.TransportError) as exc:
        raise UpstreamError() from exc

    if not genre_response.data:
        raise NotFound("genre_not_found")

    try:
        genre_id = genre_response.data[0]["id"]
    except (KeyError, TypeError, IndexError) as exc:
        raise UpstreamError() from exc

    try:
        playlists_response = (
            db.table("genre_playlists")
            .select("category")
            .eq("genre_id", genre_id)
            .execute()
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (APIError, httpx.TransportError) as exc:
        raise UpstreamError() from exc

    try:
        categories = {
            row["category"]
            for row in playlists_response.data
            if row["category"] is not None
        }
    except (KeyError, TypeError) as exc:
        raise UpstreamError() from exc

    if not categories:
        raise ResourceEmpty("no_categories")

    try:
        return GenreCategoryList(categories=sorted(categories))
    except (ValidationError, TypeError) as exc:
        raise UpstreamError() from exc


def get_genre_playlist_tracks(db: Client, playlist_id: str) -> GenrePlaylistTrackList:
    try:
        playlist_response = (
            db.table("genre_playlists").select("id").eq("id", playlist_id).execute()
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (APIError, httpx.TransportError) as exc:
        raise UpstreamError() from exc

    if not playlist_response.data:
        raise NotFound("playlist_not_found")

    try:
        playlist_tracks_response = (
            db.table("genre_playlist_tracks")
            .select("track_id, position")
            .eq("playlist_id", playlist_id)
            .order("position")
            .limit(500)
            .execute()
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (APIError, httpx.TransportError) as exc:
        raise UpstreamError() from exc

    if not playlist_tracks_response.data:
        raise ResourceEmpty("no_tracks")

    try:
        ordered_ids = [row["track_id"] for row in playlist_tracks_response.data]
        positions = [row["position"] for row in playlist_tracks_response.data]
    except (KeyError, TypeError) as exc:
        raise UpstreamError() from exc

    try:
        tracks_response = (
            db.table("tracks")
            .select(
                "track_id, title, artists, album, album_id, "
                "duration_seconds, thumbnail_url"
            )
            .in_("track_id", ordered_ids)
            .execute()
        )
    except httpx.TimeoutException as exc:
        raise UpstreamTimeout() from exc
    except (APIError, httpx.TransportError) as exc:
        raise UpstreamError() from exc

    try:
        tracks_by_id = {row["track_id"]: row for row in tracks_response.data}
    except (KeyError, TypeError) as exc:
        raise UpstreamError() from exc

    try:
        tracks = [
            GenrePlaylistTrack(**tracks_by_id[track_id], position=position)
            for track_id, position in zip(ordered_ids, positions, strict=True)
        ]
    except KeyError as exc:
        raise UpstreamError() from exc
    except (ValidationError, TypeError) as exc:
        raise UpstreamError() from exc

    return GenrePlaylistTrackList(tracks=tracks)
