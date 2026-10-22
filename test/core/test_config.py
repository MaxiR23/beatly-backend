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
# - The two gzip_* fields default to 1024 and 6, reject out-of-range
#   values naming the field without echoing them, and are read from the
#   environment
#
# What is covered:
# - Config validation for a malformed REDIS_URL, and the untouched
#   default
# - Config validation for the Supabase timeout fields
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

    settings = Settings(**_SUPABASE_KWARGS)

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


def test_gzip_settings_default_to_1024_and_6(monkeypatch):
    monkeypatch.delenv("GZIP_MINIMUM_SIZE", raising=False)
    monkeypatch.delenv("GZIP_COMPRESSLEVEL", raising=False)

    settings = Settings(**_SUPABASE_KWARGS)

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
