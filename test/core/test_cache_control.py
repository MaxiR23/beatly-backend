# test/core/test_cache_control.py
#
# Tests for the Cache-Control header helpers.
#
# Tested:
# - set_max_age writes max-age=<remaining> with the remaining seconds
# - set_max_age writes max-age=0 for 0, not no-store
# - set_max_age writes no-store when the remaining time is None
# - set_private_max_age writes private, max-age=<remaining>, and max-age=0
#   for 0
# - set_private_max_age writes no-store when the remaining time is None
# - cap_max_age lowers a plain max-age to the ceiling, never raises it, and
#   leaves private max-age, private no-cache, no-store and a missing header
#   as they are
# - private_no_cache writes "private, no-cache"
#
# What is covered:
# - The header value of each helper, as a single header
#
# Run with: pytest test/core/test_cache_control.py -v
#
# SEE: core/cache_control.py

import pytest
from fastapi import Response

from core.cache_control import (
    cap_max_age,
    private_no_cache,
    set_max_age,
    set_private_max_age,
)


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


def test_set_private_max_age_writes_private_remaining_seconds():
    response = Response()

    set_private_max_age(response, 1234)

    assert _values(response) == ["private, max-age=1234"]


def test_set_private_max_age_zero_writes_private_max_age_zero():
    response = Response()

    set_private_max_age(response, 0)

    assert _values(response) == ["private, max-age=0"]


def test_set_private_max_age_none_writes_no_store():
    response = Response()

    set_private_max_age(response, None)

    assert _values(response) == ["no-store"]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("max-age=86400", "max-age=5"),
        ("max-age=3", "max-age=3"),
        ("max-age=5", "max-age=5"),
        ("max-age=0", "max-age=0"),
        ("private, max-age=600", "private, max-age=600"),
        ("private, no-cache", "private, no-cache"),
        ("no-store", "no-store"),
        (None, None),
    ],
)
def test_cap_max_age_only_lowers_a_plain_max_age(header, expected):
    response = Response()
    if header is not None:
        response.headers["Cache-Control"] = header

    cap_max_age(response, 5)

    assert response.headers.get("cache-control") == expected
