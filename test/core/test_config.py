# test/core/test_config.py
#
# Tests for application settings, in particular the redis_url scheme
# guard.
#
# Tested:
# - A redis_url with a valid scheme (redis://) is accepted
# - A redis_url with no scheme (e.g. "localhost:6379") raises
#   ValidationError and never constructs a Settings instance
# - The default redis_url passes the same validation
# - A malformed redis_url carrying a credential never leaks that
#   credential into the ValidationError text (hide_input_in_errors)
# - The six supabase_* timeout and budget fields default to the ADR 018
#   values, reject zero and negative values naming the field, never echo
#   the rejected value, and are read from the environment
# - The four provider_* fields default to 3.05 / 10 / 12 / 2 (ADR 024), reject
#   zero and negative values naming the field without echoing them, are read
#   from the environment, and fail when connect + read or the request budget
#   reach the client's 15 s
# - The two gzip_* fields default to 1024 and 6, reject out-of-range
#   values naming the field without echoing them, and are read from the
#   environment
# - The twelve image_color_* fields default to the ADR 021 values, reject zero
#   and negative numbers naming the field without echoing them, normalize the
#   hosts to lowercase, reject a background that is not #rrggbb in lowercase,
#   fail at startup when the two contrast limits cannot both be met, fail
#   when image_color_abandoned_after is not above the fetch timeout or is
#   above the retry window, and are read from the environment
# - The eleven recommendations_* fields default to the ADR 025 values, reject
#   zero, negative and non-integer day counts, a play threshold below 1,
#   out-of-range history albums and workers, never echo the rejected value, fail when the play
#   thresholds or the tier days are not strictly ordered, and are read from
#   the environment
#
# What is covered:
# - Config validation for a malformed REDIS_URL, and the untouched
#   default
# - Config validation for the Supabase timeout fields
# - Config validation for the provider_* limits
# - Config validation for the gzip_minimum_size and gzip_compresslevel
#   fields
#
# Run with: pytest test/core/test_config.py -v
#
# SEE: core/config.py

import pytest
from pydantic import ValidationError

from core.config import Settings

_TIMEOUT_FIELDS = [
    "supabase_connect_timeout",
    "supabase_read_timeout",
    "supabase_write_timeout",
    "supabase_pool_timeout",
    "supabase_request_budget",
    "supabase_slow_request_threshold",
]

_PROVIDER_FIELDS = [
    "provider_connect_timeout",
    "provider_read_timeout",
    "provider_request_budget",
    "provider_slow_request_threshold",
]

_SUPABASE_KWARGS = {
    "supabase_url": "https://example.supabase.co",
    "supabase_service_role_key": "service-role-key",
    "supabase_jwt_secret": "jwt-secret",
    "supabase_anon_key": "anon-key",
}


def test_valid_redis_url_scheme_is_accepted():
    settings = Settings(redis_url="redis://localhost:6379/0", **_SUPABASE_KWARGS)

    assert settings.redis_url == "redis://localhost:6379/0"


def test_redis_url_without_a_scheme_raises_validation_error():
    with pytest.raises(ValidationError) as exc_info:
        Settings(redis_url="localhost:6379", **_SUPABASE_KWARGS)

    assert "redis_url" in str(exc_info.value)


def test_default_redis_url_passes_validation():
    settings = Settings(**_SUPABASE_KWARGS)

    assert settings.redis_url == "redis://localhost:6379/0"


def test_malformed_redis_url_credentials_are_not_leaked_in_the_error():
    credential = "cacheuser:sup3rs3cr3t"

    with pytest.raises(ValidationError) as exc_info:
        Settings(redis_url=f"{credential}@redis.internal:6379", **_SUPABASE_KWARGS)

    assert credential not in str(exc_info.value)


def test_supabase_timeouts_default_to_the_adr_values(monkeypatch):
    for field in _TIMEOUT_FIELDS:
        monkeypatch.delenv(field.upper(), raising=False)

    settings = Settings(_env_file=None, **_SUPABASE_KWARGS)

    assert settings.supabase_connect_timeout == 2.0
    assert settings.supabase_read_timeout == 4.0
    assert settings.supabase_write_timeout == 2.0
    assert settings.supabase_pool_timeout == 2.0
    assert settings.supabase_request_budget == 10.0
    assert settings.supabase_slow_request_threshold == 1.0


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
def test_supabase_timeout_of_zero_or_negative_fails_settings(field, value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: value}, **_SUPABASE_KWARGS)

    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
def test_invalid_supabase_timeout_is_not_echoed_in_the_error(field):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: -98765.4321}, **_SUPABASE_KWARGS)

    assert "98765.4321" not in str(exc_info.value)


def test_supabase_timeout_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_READ_TIMEOUT", "7.5")

    settings = Settings(**_SUPABASE_KWARGS)

    assert settings.supabase_read_timeout == 7.5


def test_provider_settings_have_their_defaults(monkeypatch):
    for field in _PROVIDER_FIELDS:
        monkeypatch.delenv(field.upper(), raising=False)

    settings = Settings(_env_file=None, **_SUPABASE_KWARGS)

    assert settings.provider_connect_timeout == 3.05
    assert settings.provider_read_timeout == 10.0
    assert settings.provider_request_budget == 12.0
    assert settings.provider_slow_request_threshold == 2.0


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("field", _PROVIDER_FIELDS)
def test_provider_setting_of_zero_or_negative_fails_settings(field, value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: value}, **_SUPABASE_KWARGS)

    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", _PROVIDER_FIELDS)
def test_invalid_provider_setting_is_not_echoed_in_the_error(field):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: -98765.4321}, **_SUPABASE_KWARGS)

    assert "98765.4321" not in str(exc_info.value)


def test_provider_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PROVIDER_READ_TIMEOUT", "7.5")

    settings = Settings(**_SUPABASE_KWARGS)

    assert settings.provider_read_timeout == 7.5


@pytest.mark.parametrize(("connect", "read"), [(5.0, 10.0), (3.05, 11.95), (1.0, 14.0)])
def test_provider_connect_plus_read_at_or_over_15_fails_settings(connect, read):
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            provider_connect_timeout=connect,
            provider_read_timeout=read,
            **_SUPABASE_KWARGS,
        )

    message = str(exc_info.value)
    assert "provider_connect_timeout plus provider_read_timeout" in message
    assert str(read) not in message


def test_provider_connect_plus_read_under_15_passes():
    settings = Settings(
        provider_connect_timeout=4.9,
        provider_read_timeout=10.0,
        **_SUPABASE_KWARGS,
    )

    assert settings.provider_read_timeout == 10.0


def test_provider_request_budget_of_15_fails_settings():
    with pytest.raises(ValidationError) as exc_info:
        Settings(provider_request_budget=15.0, **_SUPABASE_KWARGS)

    message = str(exc_info.value)
    assert "provider_request_budget must be below the client's 15 s" in message
    assert "input_value" not in message


def test_provider_request_budget_under_15_passes():
    settings = Settings(provider_request_budget=14.9, **_SUPABASE_KWARGS)

    assert settings.provider_request_budget == 14.9


def test_gzip_settings_default_to_1024_and_6(monkeypatch):
    monkeypatch.delenv("GZIP_MINIMUM_SIZE", raising=False)
    monkeypatch.delenv("GZIP_COMPRESSLEVEL", raising=False)

    settings = Settings(_env_file=None, **_SUPABASE_KWARGS)

    assert settings.gzip_minimum_size == 1024
    assert settings.gzip_compresslevel == 6


@pytest.mark.parametrize(
    ("field", "value"),
    [("gzip_minimum_size", -1), ("gzip_compresslevel", 0), ("gzip_compresslevel", 10)],
)
def test_gzip_setting_out_of_range_fails_settings(field, value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: value}, **_SUPABASE_KWARGS)

    assert field in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("gzip_minimum_size", -98765), ("gzip_compresslevel", 98765)],
)
def test_gzip_setting_out_of_range_is_not_echoed_in_the_error(field, value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: value}, **_SUPABASE_KWARGS)

    assert "98765" not in str(exc_info.value)


def test_gzip_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("GZIP_MINIMUM_SIZE", "2048")
    monkeypatch.setenv("GZIP_COMPRESSLEVEL", "9")

    settings = Settings(**_SUPABASE_KWARGS)

    assert settings.gzip_minimum_size == 2048
    assert settings.gzip_compresslevel == 9


_IMAGE_COLOR_POSITIVE_FIELDS = [
    "image_color_fetch_timeout",
    "image_color_max_bytes",
    "image_color_max_pixels",
    "image_color_retry_after",
    "image_color_abandoned_after",
    "image_color_max_per_request",
    "image_color_max_parallel_fetches",
]


def test_image_color_settings_default_to_the_adr_values(monkeypatch):
    for name in list(Settings.model_fields):
        if name.startswith("image_color_"):
            monkeypatch.delenv(name.upper(), raising=False)

    settings = Settings(_env_file=None, **_SUPABASE_KWARGS)

    assert settings.image_color_allowed_hosts == []
    assert settings.image_color_fetch_timeout == 3.0
    assert settings.image_color_max_bytes == 262_144
    assert settings.image_color_max_pixels == 4_194_304
    assert settings.image_color_retry_after == 86_400
    assert settings.image_color_abandoned_after == 30
    assert settings.image_color_max_per_request == 10
    assert settings.image_color_max_parallel_fetches == 4
    assert settings.image_color_pending_max_age == 5
    assert settings.image_color_background == "#0e0e0e"
    assert settings.image_color_min_background_contrast == 1.5
    assert settings.image_color_min_white_text_contrast == 4.5


def test_image_color_pending_max_age_accepts_zero_and_rejects_negative():
    assert (
        Settings(image_color_pending_max_age=0, **_SUPABASE_KWARGS)
    ).image_color_pending_max_age == 0
    with pytest.raises(ValidationError) as exc_info:
        Settings(image_color_pending_max_age=-98765, **_SUPABASE_KWARGS)

    assert "image_color_pending_max_age" in str(exc_info.value)
    assert "98765" not in str(exc_info.value)


def test_a_retired_concurrency_variable_does_not_stop_the_startup(monkeypatch):
    monkeypatch.setenv("IMAGE_COLOR_MAX_CONCURRENCY", "2")

    settings = Settings(_env_file=None, **_SUPABASE_KWARGS)

    assert settings.image_color_max_parallel_fetches == 4


def test_defaults_ignore_a_dotenv_file_in_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("IMAGE_COLOR_ALLOWED_HOSTS", raising=False)
    (tmp_path / ".env").write_text('IMAGE_COLOR_ALLOWED_HOSTS=["cdn.example.com"]\n')
    monkeypatch.chdir(tmp_path)

    assert Settings(**_SUPABASE_KWARGS).image_color_allowed_hosts == ["cdn.example.com"]
    assert Settings(_env_file=None, **_SUPABASE_KWARGS).image_color_allowed_hosts == []


@pytest.mark.parametrize("value", [0, -98765])
@pytest.mark.parametrize("field", _IMAGE_COLOR_POSITIVE_FIELDS)
def test_image_color_number_of_zero_or_negative_fails_naming_the_field(field, value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: value}, **_SUPABASE_KWARGS)

    assert field in str(exc_info.value)
    assert "98765" not in str(exc_info.value)


@pytest.mark.parametrize("value", ["#0E0E0E", "0e0e0e", "#0e0e0"])
def test_image_color_background_must_be_lowercase_hash_rrggbb(value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(image_color_background=value, **_SUPABASE_KWARGS)

    assert "image_color_background" in str(exc_info.value)


def test_image_color_hosts_are_normalized_to_lowercase():
    settings = Settings(
        image_color_allowed_hosts=[" LH3.Example.com ", "b.example.com"],
        **_SUPABASE_KWARGS,
    )

    assert settings.image_color_allowed_hosts == ["lh3.example.com", "b.example.com"]


def test_image_color_hosts_reject_an_empty_host():
    with pytest.raises(ValidationError) as exc_info:
        Settings(image_color_allowed_hosts=["a.example.com", " "], **_SUPABASE_KWARGS)

    assert "image_color_allowed_hosts" in str(exc_info.value)


def test_image_color_contrasts_that_cannot_both_be_met_fail_settings():
    with pytest.raises(ValidationError) as exc_info:
        Settings(image_color_min_background_contrast=5, **_SUPABASE_KWARGS)

    assert "image_color_min_background_contrast" in str(exc_info.value)


def test_image_color_abandoned_after_must_be_above_the_fetch_timeout():
    with pytest.raises(ValidationError) as exc_info:
        Settings(image_color_abandoned_after=3, **_SUPABASE_KWARGS)

    assert "image_color_abandoned_after" in str(exc_info.value)
    assert (
        Settings(
            image_color_abandoned_after=4, **_SUPABASE_KWARGS
        ).image_color_abandoned_after
        == 4
    )


def test_image_color_abandoned_after_must_not_exceed_the_retry_window():
    with pytest.raises(ValidationError) as exc_info:
        Settings(image_color_abandoned_after=86_401, **_SUPABASE_KWARGS)

    assert "image_color_abandoned_after" in str(exc_info.value)
    assert (
        Settings(
            image_color_abandoned_after=86_400, **_SUPABASE_KWARGS
        ).image_color_abandoned_after
        == 86_400
    )


def test_image_color_contrast_out_of_range_fails_settings():
    with pytest.raises(ValidationError) as exc_info:
        Settings(image_color_min_white_text_contrast=22, **_SUPABASE_KWARGS)

    assert "image_color_min_white_text_contrast" in str(exc_info.value)


def test_image_color_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("IMAGE_COLOR_MAX_PER_REQUEST", "3")
    monkeypatch.setenv("IMAGE_COLOR_ALLOWED_HOSTS", '["A.example.com"]')
    monkeypatch.setenv("IMAGE_COLOR_MIN_BACKGROUND_CONTRAST", "2.0")

    settings = Settings(**_SUPABASE_KWARGS)

    assert settings.image_color_max_per_request == 3
    assert settings.image_color_allowed_hosts == ["a.example.com"]
    assert settings.image_color_min_background_contrast == 2.0


_RECOMMENDATIONS_DEFAULTS = {
    "recommendations_tier_mid_plays": 5,
    "recommendations_tier_high_plays": 20,
    "recommendations_tier_high_days": 7,
    "recommendations_tier_mid_days": 14,
    "recommendations_tier_low_days": 30,
    "recommendations_seed_window_days": 30,
    "recommendations_exclude_window_days": 90,
    "recommendations_exclude_top_artists": 20,
    "recommendations_exclude_min_plays": 3,
    "recommendations_history_albums": 3,
    "recommendations_parallel_calls": 4,
}

_RECOMMENDATIONS_DAY_FIELDS = [
    "recommendations_tier_high_days",
    "recommendations_tier_mid_days",
    "recommendations_tier_low_days",
    "recommendations_seed_window_days",
    "recommendations_exclude_window_days",
]


def test_recommendations_settings_default_to_the_adr_025_values():
    settings = Settings(**_SUPABASE_KWARGS)

    for field, expected in _RECOMMENDATIONS_DEFAULTS.items():
        assert getattr(settings, field) == expected


@pytest.mark.parametrize("field", _RECOMMENDATIONS_DAY_FIELDS)
@pytest.mark.parametrize("value", [0, -1])
def test_recommendations_days_of_zero_or_less_fail_settings(field, value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: value}, **_SUPABASE_KWARGS)

    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", _RECOMMENDATIONS_DAY_FIELDS)
def test_recommendations_non_integer_days_fail_settings(field):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: "7.5"}, **_SUPABASE_KWARGS)

    assert field in str(exc_info.value)


@pytest.mark.parametrize(("mid", "high"), [(20, 20), (21, 20)])
def test_recommendations_play_thresholds_must_strictly_increase(mid, high):
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            recommendations_tier_mid_plays=mid,
            recommendations_tier_high_plays=high,
            **_SUPABASE_KWARGS,
        )

    assert "recommendations_tier_mid_plays" in str(exc_info.value)


@pytest.mark.parametrize(
    ("high", "mid", "low"), [(14, 14, 30), (7, 30, 30), (30, 14, 7)]
)
def test_recommendations_tier_days_must_strictly_increase(high, mid, low):
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            recommendations_tier_high_days=high,
            recommendations_tier_mid_days=mid,
            recommendations_tier_low_days=low,
            **_SUPABASE_KWARGS,
        )

    assert "recommendations_tier_high_days" in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recommendations_parallel_calls", 0),
        ("recommendations_parallel_calls", 9),
        ("recommendations_history_albums", 16),
        ("recommendations_history_albums", -1),
        ("recommendations_exclude_top_artists", -1),
        ("recommendations_exclude_min_plays", 0),
        ("recommendations_exclude_min_plays", -1),
    ],
)
def test_recommendations_counts_out_of_range_fail_settings(field, value):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: value}, **_SUPABASE_KWARGS)

    assert field in str(exc_info.value)


def test_recommendations_non_integer_play_threshold_fails_settings():
    with pytest.raises(ValidationError) as exc_info:
        Settings(recommendations_exclude_min_plays="3.5", **_SUPABASE_KWARGS)

    assert "recommendations_exclude_min_plays" in str(exc_info.value)


def test_recommendations_play_threshold_of_one_is_accepted():
    settings = Settings(recommendations_exclude_min_plays=1, **_SUPABASE_KWARGS)

    assert settings.recommendations_exclude_min_plays == 1


@pytest.mark.parametrize("field", list(_RECOMMENDATIONS_DEFAULTS))
def test_invalid_recommendations_setting_is_not_echoed_in_the_error(field):
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: -98765}, **_SUPABASE_KWARGS)

    assert "98765" not in str(exc_info.value)


def test_recommendations_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("RECOMMENDATIONS_PARALLEL_CALLS", "8")
    monkeypatch.setenv("RECOMMENDATIONS_TIER_LOW_DAYS", "60")
    monkeypatch.setenv("RECOMMENDATIONS_EXCLUDE_TOP_ARTISTS", "0")
    monkeypatch.setenv("RECOMMENDATIONS_EXCLUDE_MIN_PLAYS", "5")

    settings = Settings(**_SUPABASE_KWARGS)

    assert settings.recommendations_parallel_calls == 8
    assert settings.recommendations_tier_low_days == 60
    assert settings.recommendations_exclude_top_artists == 0
    assert settings.recommendations_exclude_min_plays == 5
