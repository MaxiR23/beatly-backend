# INFO: Bug report request/response models.

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BugCategory = Literal["playback", "loading", "ui", "crash", "other"]
BugEntityType = Literal["track", "album", "artist", "playlist"]
BugStatus = Literal["open", "closed"]


class BugReport(BaseModel):
    id: str
    reporter_id: str
    category: BugCategory
    description: str
    entity_type: BugEntityType | None = None
    entity_id: str | None = None
    status: BugStatus
    created_at: str
    updated_at: str


class CreateBugReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: BugCategory
    description: str = Field(min_length=5, max_length=2000)
    entity_type: BugEntityType | None = None
    entity_id: str | None = None

    @model_validator(mode="after")
    def _validate_entity_pair(self) -> CreateBugReportRequest:
        if (self.entity_type is None) != (self.entity_id is None):
            raise ValueError("entity_type and entity_id must be sent together")
        return self


class UpdateBugReportStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BugStatus
