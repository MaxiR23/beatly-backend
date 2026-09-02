# INFO: Like request/response models.

from pydantic import BaseModel, Field


class TrackArtist(BaseModel):
    id: str
    name: str


# Denormalized track fields, shared with the activity domain so a play
# event stores the same shape a like does. SEE: models/activity.py
class TrackMetadata(BaseModel):
    title: str
    artists: list[TrackArtist] = Field(min_length=1)
    album: str
    album_id: str
    thumbnail_url: str
    duration_seconds: int | None = None


class Like(BaseModel):
    track_id: str
    title: str
    artists: list[TrackArtist]
    album: str
    album_id: str
    thumbnail_url: str
    duration_seconds: int | None = None
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class AddLikeRequest(TrackMetadata):
    track_id: str
