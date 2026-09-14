# INFO: Public share endpoints -- served with no token, on purpose.
#
# These five are the first endpoints in this repo to serve a user's own
# data (a playlist) with no Depends(get_current_user_id): they exist for
# share links opened by someone who may have no session and no app
# installed. Two of the five (album, artist) already have a public
# equivalent in spirit -- the four routes/genres.py endpoints already
# serve curated content without a token -- but /public/playlists/{id} is
# new ground: it can serve a user's own playlist. The ONLY thing that
# keeps a private playlist from a stranger is the .eq("is_public", True)
# filter inside services/playlist_service.py::get_public_playlist(). Do
# not "simplify" this router to call _get_editable_playlist or
# get_playlist instead: either would require a user_id this router never
# has, and the byte-for-byte identical 404 between a private and a
# missing playlist is the property that makes this endpoint safe to
# expose. /public/tracks/{track_id}, the fifth, needs no such guard: it
# serves the external provider's own data, not a user's.

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from supabase import Client

from core.database import get_db
from core.search_provider import SearchProvider, get_search_provider
from models.public import (
    PublicAlbum,
    PublicArtist,
    PublicGenrePlaylist,
    PublicPlaylist,
    PublicTrack,
)
from models.responses import ApiSuccess, ok_response
from services.public_service import (
    get_public_album,
    get_public_artist,
    get_public_genre_playlist,
    get_public_track,
    get_public_user_playlist,
)

router = APIRouter(prefix="/public", tags=["public"])


@router.get("/album/{album_id}", response_model=ApiSuccess[PublicAlbum])
def get_public_album_route(
    # Same pattern and same reasoning as routes/album.py: the external
    # provider requires this prefix, so rejecting anything else here is a
    # 422 before an outgoing call is spent.
    album_id: Annotated[str, Path(pattern=r"^MPRE")],
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[PublicAlbum]:
    return ok_response(get_public_album(provider, album_id))


@router.get("/artist/{artist_id}", response_model=ApiSuccess[PublicArtist])
def get_public_artist_route(
    # Same pattern as routes/artist.py: an assumption about the external
    # provider's id format, not a rule it enforces itself. SEE
    # routes/artist.py for the full reasoning.
    artist_id: Annotated[str, Path(pattern=r"^(MPLA)?UC")],
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[PublicArtist]:
    return ok_response(get_public_artist(provider, artist_id))


@router.get("/playlists/{playlist_id}", response_model=ApiSuccess[PublicPlaylist])
def get_public_playlist_route(
    playlist_id: UUID,
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[PublicPlaylist]:
    return ok_response(get_public_user_playlist(db, str(playlist_id)))


@router.get(
    "/genre-playlists/{playlist_id}", response_model=ApiSuccess[PublicGenrePlaylist]
)
def get_public_genre_playlist_route(
    playlist_id: UUID,
    db: Client = Depends(get_db),  # noqa: B008
) -> ApiSuccess[PublicGenrePlaylist]:
    return ok_response(get_public_genre_playlist(db, str(playlist_id)))


@router.get("/tracks/{track_id}", response_model=ApiSuccess[PublicTrack])
def get_public_track_route(
    # No Path(pattern=...) here, on purpose: see routes/tracks.py:19-26.
    # There, the ambiguity a pattern would resolve elsewhere (a malformed
    # id and a nonexistent one both collapsing into the same 502) is
    # already broken by the 404 probe, so a pattern would only encode our
    # own assumption about the provider's id format, with nothing to buy
    # in exchange. The same reasoning applies here unchanged.
    track_id: str,
    provider: SearchProvider = Depends(get_search_provider),  # noqa: B008
) -> ApiSuccess[PublicTrack]:
    return ok_response(get_public_track(provider, track_id))
