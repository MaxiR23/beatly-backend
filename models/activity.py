# INFO: Play event and recent activity request/response models.

from typing import Any, Literal

from pydantic import BaseModel, Field

from models.likes import TrackMetadata

RecentEntityType = Literal["album", "artist", "playlist"]


class LogPlayRequest(TrackMetadata):
    track_id: str


class PlayEvent(BaseModel):
    track_id: str
    metadata: TrackMetadata
    played_at: str


class RegisterRecentRequest(BaseModel):
    entity_type: RecentEntityType
    entity_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecentEntity(BaseModel):
    entity_type: RecentEntityType
    entity_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    played_at: str
