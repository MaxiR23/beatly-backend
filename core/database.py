# INFO: Supabase client shared across services.

from functools import lru_cache

from supabase import Client, create_client

from core.config import settings


@lru_cache
def get_supabase() -> Client:
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )


def get_db() -> Client:
    return get_supabase()
