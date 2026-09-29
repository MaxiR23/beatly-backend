# test/core/test_cache_control.py
#
# Tests for the Cache-Control header helpers.
#
# Tested:
# - set_max_age writes max-age=<remaining> with the remaining seconds
# - set_max_age writes max-age=0 for 0, not no-store
# - set_max_age writes no-store when the remaining time is None
# - private_no_cache writes "private, no-cache"
#
# What is covered:
# - The header value of each helper, as a single header
#
# Run with: pytest test/core/test_cache_control.py -v
#
# SEE: core/cache_control.py

from fastapi import Response

from core.cache_control import private_no_cache, set_max_age


def _values(response):
    return response.headers.getlist("cache-control")


def test_set_max_age_writes_remaining_seconds():
    response = Response()

    set_max_age(response, 1234)

    assert _values(response) == ["max-age=1234"]


def test_set_max_age_zero_writes_max_age_zero():
    response = Response()

    set_max_age(response, 0)

    assert _values(response) == ["max-age=0"]


def test_set_max_age_none_writes_no_store():
    response = Response()

    set_max_age(response, None)

    assert _values(response) == ["no-store"]


def test_private_no_cache_writes_private_no_cache():
    response = Response()

    private_no_cache(response)

    assert _values(response) == ["private, no-cache"]
