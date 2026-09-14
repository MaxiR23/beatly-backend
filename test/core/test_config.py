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
#
# What is covered:
# - Config validation for a malformed REDIS_URL, and the untouched
#   default
#
# Run with: pytest test/core/test_config.py -v
#
# SEE: core/config.py

import pytest
from pydantic import ValidationError

from core.config import Settings

_SUPABASE_KWARGS = {
    "supabase_url": "https://example.supabase.co",
    "supabase_service_role_key": "service-role-key",
    "supabase_jwt_secret": "jwt-secret",
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
