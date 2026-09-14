# test/core/test_cache.py
#
# Tests for the Redis cache helpers.
#
# Tested:
# - cache_get returns the model deserialized from cache.get(key) on a hit
# - cache_get returns None and logs exactly one DEBUG on a miss
#   (cache.get returns None)
# - cache_get returns None, logs exactly one WARNING and does not
#   propagate when cache.get raises a RedisError
# - cache_get returns None, logs exactly one WARNING and does not raise
#   ValidationError when the cached bytes do not deserialize
# - The miss DEBUG and the failure WARNING are never the same logger
#   call: different level, different message
# - cache_set calls cache.set(key, value.model_dump_json(), ex=ttl) with
#   the exact arguments
# - cache_set logs exactly one WARNING and does not propagate when
#   cache.set raises a RedisError
# - cache_key builds "beatly:v1:{domain}:{identifier}"
# - hashed_key normalizes case, surrounding whitespace and internal
#   whitespace (tabs included) to the same key, truncates to 200 chars
#   before hashing, and always emits a 64-hex-char digest
#
# What is covered:
# - Cache hit, cache miss, Redis failure on read and on write, a
#   corrupted cached value, the two key builders
#
# Run with: pytest test/core/test_cache.py -v
#
# SEE: core/cache.py

import logging
from unittest.mock import MagicMock

from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError

from core.cache import cache_get, cache_key, cache_set, hashed_key


class _Widget(BaseModel):
    name: str
    count: int


# --- cache_get ---------------------------------------------------------


def test_cache_get_hit_deserializes_to_the_model():
    cache = MagicMock()
    cache.get.return_value = b'{"name": "gadget", "count": 3}'

    result = cache_get(cache, "beatly:v1:widget:1", _Widget)

    assert result == _Widget(name="gadget", count=3)


def test_cache_get_miss_returns_none_and_logs_one_debug(caplog):
    cache = MagicMock()
    cache.get.return_value = None

    with caplog.at_level(logging.DEBUG, logger="core.cache"):
        result = cache_get(cache, "beatly:v1:widget:1", _Widget)

    assert result is None
    records = [r for r in caplog.records if r.name == "core.cache"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert "miss" in records[0].getMessage()


def test_cache_get_redis_error_returns_none_and_logs_one_warning(caplog):
    cache = MagicMock()
    cache.get.side_effect = RedisConnectionError("connection refused")

    with caplog.at_level(logging.DEBUG, logger="core.cache"):
        result = cache_get(cache, "beatly:v1:widget:1", _Widget)

    assert result is None
    records = [r for r in caplog.records if r.name == "core.cache"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "ConnectionError" in records[0].getMessage()
    # The exception's message is never logged, only its class name.
    assert "connection refused" not in records[0].getMessage()


def test_cache_get_corrupted_value_returns_none_and_logs_warning_not_raise(
    caplog,
):
    cache = MagicMock()
    cache.get.return_value = b"{"

    with caplog.at_level(logging.DEBUG, logger="core.cache"):
        result = cache_get(cache, "beatly:v1:widget:1", _Widget)

    assert result is None
    records = [r for r in caplog.records if r.name == "core.cache"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "ValidationError" in records[0].getMessage()


def test_cache_get_miss_and_failure_are_not_the_same_logger_call(caplog):
    miss_cache = MagicMock()
    miss_cache.get.return_value = None
    failure_cache = MagicMock()
    failure_cache.get.side_effect = RedisConnectionError()

    with caplog.at_level(logging.DEBUG, logger="core.cache"):
        cache_get(miss_cache, "beatly:v1:widget:1", _Widget)
        miss_record = next(r for r in caplog.records if r.name == "core.cache")
        caplog.clear()
        cache_get(failure_cache, "beatly:v1:widget:1", _Widget)
        failure_record = next(r for r in caplog.records if r.name == "core.cache")

    assert miss_record.levelno != failure_record.levelno
    assert miss_record.getMessage() != failure_record.getMessage()


# --- cache_set ----------------------------------------------------------


def test_cache_set_calls_set_with_exact_arguments():
    cache = MagicMock()
    value = _Widget(name="gadget", count=3)

    cache_set(cache, "beatly:v1:widget:1", value, ttl=86400)

    cache.set.assert_called_once_with(
        "beatly:v1:widget:1", value.model_dump_json(), ex=86400
    )


def test_cache_set_redis_error_does_not_propagate_and_logs_warning(caplog):
    cache = MagicMock()
    cache.set.side_effect = RedisConnectionError("connection refused")
    value = _Widget(name="gadget", count=3)

    with caplog.at_level(logging.DEBUG, logger="core.cache"):
        cache_set(cache, "beatly:v1:widget:1", value, ttl=86400)

    records = [r for r in caplog.records if r.name == "core.cache"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "ConnectionError" in records[0].getMessage()
    assert "connection refused" not in records[0].getMessage()


# --- cache_key / hashed_key ----------------------------------------------


def test_cache_key_builds_prefixed_key():
    assert cache_key("album", "MPREb_123") == "beatly:v1:album:MPREb_123"


def test_hashed_key_normalizes_case_and_whitespace_to_the_same_key():
    plain = hashed_key("search", "beatles the")
    upper = hashed_key("search", "Beatles the")
    trailing_space = hashed_key("search", "beatles the ")
    tabs_and_case = hashed_key("search", "  BEATLES\tthe  ")

    assert plain == upper == trailing_space == tabs_and_case


def test_hashed_key_different_text_produces_different_key():
    assert hashed_key("search", "beatles") != hashed_key("search", "rolling stones")


def test_hashed_key_truncates_before_hashing():
    long_query = "a" * 500
    truncated_query = "a" * 200

    assert hashed_key("search", long_query) == hashed_key("search", truncated_query)


def test_hashed_key_always_emits_a_64_hex_char_digest():
    key = hashed_key("search", "some query")
    digest = key.removeprefix("beatly:v1:search:")

    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
