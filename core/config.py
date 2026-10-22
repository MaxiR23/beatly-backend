# INFO: Application settings loaded from environment variables.

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
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
    supabase_anon_key: str

    # Seconds, all > 0 (a zero or negative value fails Settings() at startup).
    # The per-phase timeouts and the per-request budget the transport uses
    # to decide on its one GET retry; the numbers and their arithmetic are
    # in ADR 018. supabase_slow_request_threshold is the duration above
    # which core/supabase_transport.py logs a request.
    supabase_connect_timeout: float = Field(default=2.0, gt=0)
    supabase_read_timeout: float = Field(default=4.0, gt=0)
    supabase_write_timeout: float = Field(default=2.0, gt=0)
    supabase_pool_timeout: float = Field(default=2.0, gt=0)
    supabase_request_budget: float = Field(default=10.0, gt=0)
    supabase_slow_request_threshold: float = Field(default=1.0, gt=0)

    # The two parameters of GZipMiddleware in app.py. gzip_minimum_size is
    # the body size in bytes below which a response is not compressed
    # (under ~1 KB it fits in one TCP segment either way; see "Compression"
    # in docs/api/conventions.md). gzip_compresslevel is the zlib level,
    # 1-9: 6 because 9 shrinks under 1% more for ~20% more CPU. A value out
    # of range fails Settings() at startup.
    gzip_minimum_size: int = Field(default=1024, ge=0)
    gzip_compresslevel: int = Field(default=6, ge=1, le=9)

    # Unlike the four required Supabase fields (supabase_url to
    # supabase_anon_key), its absence degrades to "no
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
