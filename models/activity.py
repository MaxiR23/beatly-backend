# INFO: Play event and recent activity request/response models.

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from models.likes import TrackMetadata

RecentEntityType = Literal["album", "artist", "playlist"]


class LogPlayRequest(TrackMetadata):
    track_id: str


class PlayEvent(BaseModel):
    track_id: str
    metadata: TrackMetadata
    played_at: str


class RecentMetadata(BaseModel):
    # Same three-key shape that migration 036 leaves on converted rows. The
    # thumbnail_url is not validated as a URL on purpose (decision 3 of #166).
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    subtitle: str | None = None
    thumbnail_url: str | None = None


class RegisterRecentRequest(BaseModel):
    entity_type: RecentEntityType
    entity_id: str
    metadata: RecentMetadata


class RecentEntity(BaseModel):
    entity_type: RecentEntityType
    entity_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    played_at: str
