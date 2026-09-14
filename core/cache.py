# INFO: Redis client and cache helpers shared across services.

import hashlib
import logging
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from redis import Redis
from redis.exceptions import RedisError

from core.config import settings

# setup_logging() is called once, in app.py, and is not called again here.
logger = logging.getLogger(__name__)

# Neutral alias so services and routers can annotate `cache: CacheClient`
# without naming the provider by its brand, the same way
# core/search_provider.py exposes `SearchProvider = YTMusic`.
CacheClient = Redis

# The library's default is None, i.e. block forever. A Redis that is
# hanging (not rejecting: a rejection fails instantly) with no timeout
# would leave the threadpool's worker thread waiting forever, which is
# worse than having no cache at all and would break the criterion that a
# Redis failure must never affect availability. With a timeout set, the
# library raises redis.exceptions.TimeoutError, which is a RedisError, and
# the caller falls back to the provider.
_SOCKET_TIMEOUT_SECONDS = 0.25

_KEY_PREFIX = "beatly:v1"
_MAX_KEY_TEXT_CHARS = 200


@lru_cache
def _redis() -> CacheClient:
    return Redis.from_url(
        settings.redis_url,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
    )


def get_redis() -> CacheClient:
    return _redis()


def cache_key(domain: str, identifier: str) -> str:
    return f"{_KEY_PREFIX}:{domain}:{identifier}"


def hashed_key(domain: str, text: str) -> str:
    # Lowercase, trim and collapse internal whitespace in one operation:
    # str.split() with no argument also collapses tabs, newlines and the
    # rest of unicode whitespace, not just plain spaces.
    normalized = " ".join(text.lower().split())[:_MAX_KEY_TEXT_CHARS]
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return cache_key(domain, digest)


ModelT = TypeVar("ModelT", bound=BaseModel)


def cache_get(cache: CacheClient, key: str, model: type[ModelT]) -> ModelT | None:
    # No decode_responses=True on the client on purpose: with raw bytes, a
    # value with invalid UTF-8 is rejected by pydantic as a
    # ValidationError, which is exactly the "cache failure" branch already
    # decided below. With decode_responses=True it would instead raise
    # UnicodeDecodeError, which is neither RedisError nor ValidationError
    # and would surface as a 500.
    try:
        raw = cache.get(key)
        if raw is None:
            logger.debug("cache miss key=%s", key)
            return None
        return model.model_validate_json(raw)
    # This is not the antipattern CLAUDE.md prohibits (an except that
    # swallows an error and returns an empty value): it returns exactly
    # the same None a miss returns, so the caller does exactly what it
    # would do with no cache at all, and the failure is not hidden -- the
    # WARNING line below stays. Only the exception's class name is
    # logged, never its message and never logger.exception: precedent is
    # core/search_provider.py's own caught-failure log, and a stack trace
    # per request during a Redis outage would flood the log without
    # adding information.
    except (RedisError, ValidationError) as exc:
        logger.warning("cache read failed key=%s: %s", key, type(exc).__name__)
        return None


def cache_set(cache: CacheClient, key: str, value: BaseModel, *, ttl: int) -> None:
    try:
        cache.set(key, value.model_dump_json(), ex=ttl)
    except RedisError as exc:
        logger.warning("cache write failed key=%s: %s", key, type(exc).__name__)
