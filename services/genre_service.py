# INFO: Reads genres from Supabase.

import httpx
from postgrest.exceptions import APIError
from pydantic import ValidationError
from supabase import Client

from core.exceptions import NotFound, ResourceEmpty, UpstreamError, UpstreamTimeout
from models.genres import Genre, GenreList, GenrePlaylist, GenrePlaylistList


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
