# INFO: Reads and updates the authenticated user's profile in Supabase.

from postgrest.exceptions import APIError
from supabase import Client

from core.exceptions import Conflict, NotFound
from core.upstream import translate_upstream_errors
from models.profiles import Profile, UpdateProfileRequest

_COLUMNS = "id, role, username, display_name, avatar_url, created_at, updated_at"


def get_profile(db: Client, user_id: str) -> Profile:
    with translate_upstream_errors():
        response = db.table("profiles").select(_COLUMNS).eq("id", user_id).execute()

        if not response.data:
            raise NotFound("profile_not_found")

        return Profile(**response.data[0])


def update_profile(db: Client, user_id: str, payload: UpdateProfileRequest) -> Profile:
    fields = payload.model_dump(exclude_unset=True)

    if not fields:
        return get_profile(db, user_id)

    with translate_upstream_errors():
        try:
            response = (
                db.table("profiles")
                .update(fields)
                .eq("id", user_id)
                .select(_COLUMNS)
                .execute()
            )
        except APIError as exc:
            if exc.code == "23505":
                raise Conflict("username_taken") from exc
            raise

        if not response.data:
            raise NotFound("profile_not_found")

        return Profile(**response.data[0])
