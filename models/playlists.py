# INFO: Playlist request/response models.

from pydantic import BaseModel, Field, field_validator, model_validator

from models.likes import TrackArtist


class Playlist(BaseModel):
    id: str
    owner_id: str
    title: str
    description: str | None = None
    is_public: bool = False
    created_at: str
    updated_at: str

    @field_validator("is_public", mode="before")
    @classmethod
    def _null_is_public_is_false(cls, value: object) -> object:
        # The column is nullable with a default of false. Rows written by
        # this API always set it, but a row predating the domain can hold
        # a literal null, which means the same thing as false.
        return False if value is None else value


class PlaylistList(BaseModel):
    playlists: list[Playlist]


# Catalog fields read from public.tracks. Both ids are exposed on
# purpose: id is the uuid playlist_tracks joins on, track_id the provider
# id the likes and activity domains key on.
class PlaylistTrack(BaseModel):
    id: str
    track_id: str
    title: str
    artists: list[TrackArtist] = Field(min_length=1)
    album: str
    album_id: str
    duration_seconds: int
    thumbnail_url: str
    position: int


class PlaylistDetail(Playlist):
    tracks: list[PlaylistTrack]
    total_count: int
    has_more: bool


class CreatePlaylistRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    is_public: bool = False


class UpdatePlaylistRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    is_public: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_null_title(cls, data: object) -> object:
        # title is non-nullable on Playlist: omitting it from the body
        # leaves it unchanged, but an explicit null must be rejected here
        # rather than reaching the database as an invalid update.
        if isinstance(data, dict) and data.get("title", False) is None:
            raise ValueError("title must not be null")
        return data
