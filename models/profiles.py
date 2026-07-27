# INFO: Profile response model and the role hierarchy used by require_role.

from enum import StrEnum

from pydantic import BaseModel


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
