# INFO: Like request/response models.

from pydantic import BaseModel, Field


class TrackArtist(BaseModel):
    id: str
    name: str


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


class LikeList(BaseModel):
    likes: list[Like]


class AddLikeRequest(BaseModel):
    track_id: str
    title: str
    artists: list[TrackArtist] = Field(min_length=1)
    album: str
    album_id: str
    thumbnail_url: str
    duration_seconds: int | None = None
