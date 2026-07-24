# INFO: Application settings loaded from environment variables.

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Beatly API"
    env: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    supabase_url: str
    supabase_service_role_key: str


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
