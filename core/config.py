# INFO: Application settings loaded from environment variables.

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.dominant_color import hex_to_rgb, relative_luminance

_REDIS_URL_SCHEMES = ("redis://", "rediss://", "unix://")

# The app's own HTTP client gives up on a request after 15 000 ms. That client
# is not in this repo (see ADR 018 and 024); the provider limits below must
# stay under it.
_CLIENT_REQUEST_TIMEOUT = 15.0


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

    # Seconds, all > 0 (ADR 024). The external provider's per-call (connect,
    # read) timeouts, the budget all the provider calls of one incoming
    # request share, and the duration above which one provider request is
    # logged. Measured on 2026-10-28 (160 requests): max 1.0 s, p95 0.805 s.
    # Read 10 s is ~10x that max; connect 3.05 s is just above the 3 s TCP
    # retransmission window; the slow threshold sits above the measured max.
    # connect + read and the budget must each stay under the client's 15 s.
    provider_connect_timeout: float = Field(default=3.05, gt=0)
    provider_read_timeout: float = Field(default=10.0, gt=0)
    provider_request_budget: float = Field(default=12.0, gt=0)
    provider_slow_request_threshold: float = Field(default=2.0, gt=0)

    # The two parameters of GZipMiddleware in app.py. gzip_minimum_size is
    # the body size in bytes below which a response is not compressed
    # (under ~1 KB it fits in one TCP segment either way; see "Compression"
    # in docs/api/conventions.md). gzip_compresslevel is the zlib level,
    # 1-9: 6 because 9 shrinks under 1% more for ~20% more CPU. A value out
    # of range fails Settings() at startup.
    gzip_minimum_size: int = Field(default=1024, ge=0)
    gzip_compresslevel: int = Field(default=6, ge=1, le=9)

    # Image colors (ADR 021). The allowed hosts are the only ones the color
    # job ever downloads from (exact hostname match, no wildcards); the
    # fetch limits bound one download; retry_after and max_per_request bound
    # how often and how much is computed, max_parallel_fetches is the size of
    # the per-process pool that downloads (the one limit on simultaneous
    # downloads) and pending_max_age caps, in seconds, the max-age of a
    # response that still carries a color to be computed. abandoned_after is
    # the seconds a claim without color and without failure takes to count as
    # abandoned and be claimed again (ADR 022). The last
    # three fields adjust the dominant color to the dark theme: the
    # background of the app, the minimum WCAG contrast against it, and the
    # minimum contrast white text must keep on top of the color.
    # The hosts come only from IMAGE_COLOR_ALLOWED_HOSTS. With the list
    # empty nothing is downloaded and every color is null.
    image_color_allowed_hosts: list[str] = []
    image_color_fetch_timeout: float = Field(default=3.0, gt=0)
    image_color_max_bytes: int = Field(default=262_144, gt=0)
    image_color_max_pixels: int = Field(default=4_194_304, gt=0)
    image_color_retry_after: int = Field(default=86_400, gt=0)
    image_color_abandoned_after: int = Field(default=30, gt=0)
    image_color_max_per_request: int = Field(default=10, ge=1)
    image_color_max_parallel_fetches: int = Field(default=4, ge=1)
    image_color_pending_max_age: int = Field(default=5, ge=0)
    image_color_background: str = Field(default="#0e0e0e", pattern=r"^#[0-9a-f]{6}$")
    image_color_min_background_contrast: float = Field(default=1.5, ge=1, le=21)
    image_color_min_white_text_contrast: float = Field(default=4.5, ge=1, le=21)

    # Personalized recommendations (ADR 025). The tier thresholds and days
    # decide how long a generated set stays valid: the more the user played
    # since the previous generation, the sooner it expires (mid_plays <
    # high_plays, and high_days < mid_days < low_days). seed_window_days is
    # the window of the listened seeds, of the history albums and of the
    # play count of a first generation; exclude_window_days is the window of
    # the top (exclude_top_artists) and of the threshold (exclude_min_plays):
    # an artist in that top or with at least that many plays in the window
    # is never recommended.
    # history_albums caps the album seeds taken from the play history.
    # parallel_calls is the number of simultaneous provider calls in one
    # generation (8 is the most that was measured).
    recommendations_tier_mid_plays: int = Field(default=5, gt=0)
    recommendations_tier_high_plays: int = Field(default=20, gt=0)
    recommendations_tier_high_days: int = Field(default=7, gt=0)
    recommendations_tier_mid_days: int = Field(default=14, gt=0)
    recommendations_tier_low_days: int = Field(default=30, gt=0)
    recommendations_seed_window_days: int = Field(default=30, gt=0)
    recommendations_exclude_window_days: int = Field(default=90, gt=0)
    recommendations_exclude_top_artists: int = Field(default=20, ge=0)
    recommendations_exclude_min_plays: int = Field(default=3, gt=0)
    recommendations_history_albums: int = Field(default=3, ge=0, le=15)
    recommendations_parallel_calls: int = Field(default=4, ge=1, le=8)

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

    @field_validator("image_color_allowed_hosts")
    @classmethod
    def _normalize_image_color_hosts(cls, value: list[str]) -> list[str]:
        hosts = [host.strip().lower() for host in value]
        if not all(hosts):
            raise ValueError("image_color_allowed_hosts must not contain empty hosts")
        return hosts

    # Fails at startup when no gray can satisfy both contrast limits: the
    # lightest gray that clears the contrast against the background must
    # still be dark enough for white text. The message carries no value.
    @model_validator(mode="after")
    def _validate_image_color_contrasts(self) -> Settings:
        background = relative_luminance(hex_to_rgb(self.image_color_background))
        lightest_needed = self.image_color_min_background_contrast * (background + 0.05)
        lightest_allowed = 1.05 / self.image_color_min_white_text_contrast
        if lightest_needed > lightest_allowed:
            raise ValueError(
                "image_color_min_background_contrast and "
                "image_color_min_white_text_contrast cannot both be met"
            )
        return self

    # The seconds a claim without color and without failure takes to count as
    # abandoned. Above the fetch timeout: a job that is already downloading
    # can take that long on its own, and a lower threshold would duplicate
    # every slow download. At most the retry window: a larger value would
    # never fire before the 24 h window takes the key back, and would mean
    # nothing. The message carries no value.
    @model_validator(mode="after")
    def _validate_image_color_abandoned_after(self) -> Settings:
        if not (
            self.image_color_fetch_timeout
            < self.image_color_abandoned_after
            <= self.image_color_retry_after
        ):
            raise ValueError(
                "image_color_abandoned_after must be above "
                "image_color_fetch_timeout and at most image_color_retry_after"
            )
        return self

    # A single call outside any budget (a script) must not wait longer than
    # the app does. The message carries no value.
    @model_validator(mode="after")
    def _validate_provider_call_timeouts(self) -> Settings:
        if (
            self.provider_connect_timeout + self.provider_read_timeout
            >= _CLIENT_REQUEST_TIMEOUT
        ):
            raise ValueError(
                "provider_connect_timeout plus provider_read_timeout "
                "must be below the client's 15 s"
            )
        return self

    @model_validator(mode="after")
    def _validate_provider_request_budget(self) -> Settings:
        if self.provider_request_budget >= _CLIENT_REQUEST_TIMEOUT:
            raise ValueError("provider_request_budget must be below the client's 15 s")
        return self

    # The messages carry no value.
    @model_validator(mode="after")
    def _validate_recommendation_tier_plays(self) -> Settings:
        if self.recommendations_tier_mid_plays >= self.recommendations_tier_high_plays:
            raise ValueError(
                "recommendations_tier_mid_plays must be below "
                "recommendations_tier_high_plays"
            )
        return self

    @model_validator(mode="after")
    def _validate_recommendation_tier_days(self) -> Settings:
        if not (
            self.recommendations_tier_high_days
            < self.recommendations_tier_mid_days
            < self.recommendations_tier_low_days
        ):
            raise ValueError(
                "recommendations_tier_high_days, recommendations_tier_mid_days "
                "and recommendations_tier_low_days must be strictly increasing"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
