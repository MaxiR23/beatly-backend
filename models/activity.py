# INFO: Play event and recent activity request/response models.

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from models.likes import TrackMetadata
from models.playlists import LIKED_PLAYLIST_ID

RecentEntityType = Literal["album", "artist", "playlist"]
RecentPlaylistKind = Literal["user", "genre", "liked"]


class LogPlayRequest(TrackMetadata):
    track_id: str


class PlayEvent(BaseModel):
    track_id: str
    metadata: TrackMetadata
    played_at: str


class RecentMetadata(BaseModel):
    # Three keys for album and artist (the shape migration 036 leaves on
    # converted rows), four for playlist: kind is added (#168). kind is
    # excluded from the dump when None so an album or artist is still stored
    # with three keys. The thumbnail_url is not validated as a URL on purpose
    # (decision 3 of #166).
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    subtitle: str | None = None
    thumbnail_url: str | None = None
    kind: RecentPlaylistKind | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RegisterRecentRequest(BaseModel):
    entity_type: RecentEntityType
    entity_id: str
    metadata: RecentMetadata

    @model_validator(mode="after")
    def _validate_kind(self) -> RegisterRecentRequest:
        # kind depends on entity_type, so it is checked here and not on
        # RecentMetadata: required for a playlist, forbidden (even as an
        # explicit null) for album and artist, and "liked" pairs with the
        # virtual liked playlist's id and only with it (#168).
        if self.entity_type == "playlist":
            kind = self.metadata.kind
            if kind is None:
                raise ValueError("kind is required for a playlist")
            if (kind == "liked") != (self.entity_id == LIKED_PLAYLIST_ID):
                raise ValueError("kind does not match entity_id")
        elif "kind" in self.metadata.model_fields_set:
            raise ValueError("kind is only valid for a playlist")
        return self


class RecentEntity(BaseModel):
    entity_type: RecentEntityType
    entity_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    played_at: str
