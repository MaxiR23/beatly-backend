# INFO: Artist request/response models.

from pydantic import BaseModel, Field

from models.album import AlbumRef
from models.search import SearchArtistRef


class ArtistSong(BaseModel):
    # album/album_id use the same field names SearchSong already exposes
    # in /search, so the client has a single "song" mapping regardless of
    # where it came from. The two fields always travel together: if
    # album_id is null, album is too, even when the provider sent a
    # name with no id. The normalization that enforces that invariant
    # lives in services/artist_service.py, not here.
    track_id: str | None = None
    title: str
    artists: list[SearchArtistRef] = Field(default_factory=list)
    album: str | None = None
    album_id: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None


class ArtistRelease(BaseModel):
    # No artists and no audio_playlist_id: the provider's parse_single()
    # never includes those keys for a single/EP (not even empty/null), so
    # always returning them would tell the client something that is never
    # true. type (Single/EP) lives here and deliberately not on AlbumRef,
    # whose exclusion of `type` was already decided in plan-100.md.
    id: str
    title: str
    year: str | None = None
    type: str | None = None
    thumbnail_url: str | None = None


class ArtistRef(BaseModel):
    # Not SearchArtist (no thumbnail_url) and not SearchArtistRef (whose id
    # is nullable because it models a mention with no link): a related
    # artist always arrives with its browseId.
    id: str
    name: str
    thumbnail_url: str | None = None


class Artist(BaseModel):
    id: str
    name: str
    thumbnail_url: str | None = None
    songs: list[ArtistSong] = Field(default_factory=list)
    albums: list[AlbumRef] = Field(default_factory=list)
    singles: list[ArtistRelease] = Field(default_factory=list)
    related: list[ArtistRef] = Field(default_factory=list)
