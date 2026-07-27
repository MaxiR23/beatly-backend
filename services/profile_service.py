# INFO: Reads the authenticated user's profile from Supabase.

from supabase import Client

from core.exceptions import NotFound
from core.upstream import translate_upstream_errors
from models.profiles import Profile


def get_profile(db: Client, user_id: str) -> Profile:
    with translate_upstream_errors():
        response = db.table("profiles").select("id, role").eq("id", user_id).execute()

        if not response.data:
            raise NotFound("profile_not_found")

        return Profile(**response.data[0])
