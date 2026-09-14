# INFO: Application settings loaded from environment variables.

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REDIS_URL_SCHEMES = ("redis://", "rediss://", "unix://")


class Settings(BaseSettings):
    # hide_input_in_errors keeps pydantic from appending input_value=... to
    # any ValidationError raised for this class. Settings is the repo's
    # most secret-dense class (supabase_service_role_key,
    # supabase_jwt_secret, redis_url), so no field's raw value should ever
    # reach a startup log via the default error rendering.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "Beatly API"
    env: Literal["development", "production"] = "development"
    log_level: str = "INFO"

    supabase_url: str
    supabase_service_role_key: str
    supabase_jwt_secret: str

    # Unlike the three Supabase fields above, its absence degrades to "no
    # cache", never to a down service: it carries no secret (the local
    # docker-compose Redis has no password) and CI passes no REDIS_URL, so
    # a default is what keeps Settings() importable there.
    redis_url: str = "redis://localhost:6379/0"

    # Fails Settings() at startup instead of at the first request: a
    # malformed value (typically the scheme missing) does not raise
    # redis.exceptions.RedisError, so core/cache.py's own except clause
    # never sees it and it would otherwise surface as a 500 on every
    # provider endpoint. The message omits the received value on purpose:
    # a redis:// URL can carry credentials (redis://user:pass@host).
    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url_scheme(cls, value: str) -> str:
        if not value.startswith(_REDIS_URL_SCHEMES):
            raise ValueError("redis_url must start with redis://, rediss:// or unix://")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
