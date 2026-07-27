# INFO: Profile response/request models and the role hierarchy used by require_role.

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Role(StrEnum):
    USER = "user"
    TESTER = "tester"
    DEVELOPER = "developer"
    ADMIN = "admin"


ROLE_RANK: dict[Role, int] = {
    Role.USER: 0,
    Role.TESTER: 1,
    Role.DEVELOPER: 2,
    Role.ADMIN: 3,
}


class Profile(BaseModel):
    id: str
    role: Role
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: str
    updated_at: str


class UpdateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_]{3,30}$")
    display_name: str | None = None
    avatar_url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_null_username(cls, data: object) -> object:
        # username is non-nullable on Profile: omitting it from the body
        # leaves it unchanged, but an explicit null must be rejected here
        # rather than reaching the database as an invalid update.
        if isinstance(data, dict) and data.get("username", False) is None:
            raise ValueError("username must not be null")
        return data
