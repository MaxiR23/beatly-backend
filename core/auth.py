# INFO: FastAPI dependencies for resolving the authenticated user.

# Mirrors the role core/database.py plays for get_db: this module wires
# together the JWT, the database and services/profile_service.py into
# dependencies routers consume via Depends(). No business logic lives
# here, only composition.

from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client

from core.config import settings
from core.database import get_db
from core.exceptions import Forbidden, Unauthorized
from models.profiles import ROLE_RANK, Profile, Role
from services.profile_service import get_profile

_bearer_scheme = HTTPBearer(auto_error=False)


def decode_access_token(token: str) -> str:
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise Unauthorized() from exc

    user_id = payload.get("sub")
    if not user_id:
        raise Unauthorized()

    return user_id


def get_current_user_id(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> str:
    if credentials is None:
        raise Unauthorized()

    return decode_access_token(credentials.credentials)


def get_current_profile(
    user_id: Annotated[str, Depends(get_current_user_id)],
    db: Annotated[Client, Depends(get_db)],
) -> Profile:
    return get_profile(db, user_id)


def require_role(role: Role):
    def dependency(
        profile: Annotated[Profile, Depends(get_current_profile)],
    ) -> Profile:
        if ROLE_RANK[profile.role] < ROLE_RANK[role]:
            raise Forbidden()
        return profile

    return dependency
