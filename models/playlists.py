# INFO: Playlist request/response models.

from pydantic import BaseModel, Field, field_validator, model_validator

from models.likes import TrackArtist, TrackMetadata

# A batch cap, not pagination: a client with more tracks than this sends
# more than one request. Enforced by the model, so an over-long batch is
# a 422 before any database call.
BULK_TRACKS_LIMIT = 200


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


# The catalog metadata a track is written with, plus the provider id it is
# keyed on. Reuses TrackMetadata so a playlist stores the same shape a like
# and a play event do. SEE: models/likes.py
class AddPlaylistTrackRequest(TrackMetadata):
    track_id: str

    # Narrowed from TrackMetadata's int | None. PlaylistTrack.duration_seconds
    # is not nullable, so a track stored without one would read back as a 502
    # from GET /playlists/{id}, and nothing here fills it in afterwards: the
    # duration comes from the body, not from a catalog lookup. Requiring it
    # makes that a 422 before the write instead.
    duration_seconds: int


class BulkAddPlaylistTracksRequest(BaseModel):
    tracks: list[AddPlaylistTrackRequest] = Field(
        min_length=1, max_length=BULK_TRACKS_LIMIT
    )


# Counts rather than the added rows: the caller sent the metadata, so the
# only new information is how much of the batch actually landed.
class BulkAddResult(BaseModel):
    added: int
    skipped: int


class MovePlaylistTrackRequest(BaseModel):
    # Positions are 1-based, matching the move_playlist_track RPC's indices.
    # The upper bound depends on the playlist, so it is checked in the
    # service; only the lower bound can be fixed here.
    old_position: int = Field(ge=1)
    new_position: int = Field(ge=1)


class OwnedPlaylistIds(BaseModel):
    playlist_ids: list[str]
