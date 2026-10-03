# INFO: Like request/response models.

from pydantic import BaseModel, Field


class TrackArtist(BaseModel):
    id: str
    name: str


# Track metadata as the clients send it. A like does not store these fields:
# POST /likes upserts them into the catalog (tracks) and user_likes only
# references that row. The activity domain reuses the same shape for a play
# event. SEE: models/activity.py
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
