# INFO: Reads genres from Supabase.

import httpx
from postgrest.exceptions import APIError
from pydantic import ValidationError

from core.database import get_supabase
from core.exceptions import ResourceEmpty, UpstreamError, UpstreamTimeout
from models.genres import Genre, GenreList


def list_genres() -> GenreList:
    try:
        response = (
            get_supabase()
            .table("genres")
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
