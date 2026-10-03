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
    username: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: str
    updated_at: str


class UpdateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_]{3,30}$")
    # Mirrors profiles_display_name_length (017); null stays allowed (the
    # constraint admits it and it clears the name). pydantic and char_length
    # both count code points.
    display_name: str | None = Field(default=None, min_length=1, max_length=50)
    avatar_url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_null_username(cls, data: object) -> object:
        # username starts null at signup (the database trigger does not set
        # it) and Profile reads it back as null until the user picks one.
        # PATCH sets or changes it but never clears it: omitting it from the
        # body leaves it unchanged, and an explicit null is rejected here
        # rather than reaching the database as an update that would drop an
        # already claimed handle.
        if isinstance(data, dict) and data.get("username", False) is None:
            raise ValueError("username must not be null")
        return data
