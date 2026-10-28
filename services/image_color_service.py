# INFO: Reads the stored color of the images of a response and computes the
# missing ones after the response is sent (ADR 021).

# Two excepts in this module look like the antipattern CLAUDE.md prohibits (an
# except that swallows an error and returns an empty value) and are not:
# - load_image_colors catches UpstreamError and UpstreamTimeout from the read
#   of the color table and lets the response go out with color null. This is
#   the explicit exception the owner decided (B2): a color is decoration, the
#   same degradation as core/cache.py (ADR 006). The WARNING line stays.
# - compute_image_colors runs as a background task: there is no caller to
#   return anything to, and the state is not lost, it is persisted in
#   image_colors.failed_at (D7). Its excepts are in _claim and _compute_one.
#   _claim catches UpstreamError and UpstreamTimeout of the claim; _compute_one
#   catches UpstreamError and UpstreamTimeout of the write of its row and a
#   fixed tuple for the image itself (_ImageRejected, httpx.HTTPError,
#   httpx.InvalidURL, OSError, ValueError, Image.DecompressionBombError,
#   SyntaxError); an exception outside them is a bug and escapes to the ASGI
#   server log.
# The log lines carry the class name of the failure and a digest of the image
# key, never a message and never the URL.

import asyncio
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from PIL import Image
from pydantic import BaseModel
from supabase import Client

from core.config import settings
from core.dominant_color import (
    adjust_for_dark_theme,
    dominant_rgb,
    hex_to_rgb,
    to_hex,
)
from core.exceptions import UpstreamError, UpstreamTimeout
from core.image_color_scope import ImageColorScope
from core.thumbnails import image_key
from core.upstream import translate_upstream_errors
from models.images import StoredImageColor

logger = logging.getLogger(__name__)

_MAX_URL_LENGTH = 2048


# One pool per process, image_color_max_parallel_fetches threads: the only
# limit on simultaneous downloads. A background task waits for its jobs as a
# coroutine, so it does not take a thread of the threadpool the requests
# share, and no task is dropped for lack of room: its jobs wait in the queue.
@lru_cache
def _pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(
        max_workers=settings.image_color_max_parallel_fetches,
        thread_name_prefix="image-color",
    )


class _ImageRejected(Exception):
    # Not an AppError on purpose: it never reaches a handler or a client, it
    # is caught in compute_image_colors and stored as a failed attempt.
    pass


def is_color_eligible(url: str) -> bool:
    # The only gate between a URL in a response (which a client can have
    # stored through POST /likes) and a download made by the server: https,
    # an exact allowed hostname, no userinfo, the default port.
    # A malformed URL is not eligible, never an exception: this runs on the
    # path of every request. Control characters are refused before parsing
    # (urlsplit strips \t\r\n silently, and httpx rejects the rest).
    if len(url) > _MAX_URL_LENGTH:
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return False
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if hostname is None or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    if hostname not in settings.image_color_allowed_hosts:
        logger.debug("image_color skipped host=%s", hostname)
        return False
    return True


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _main_image_keys(dumped: object) -> list[str]:
    # The top-level image of the data and the tiles of its mosaic, small tier,
    # in order and without repeats. Anything else (a search result has none of
    # them at the top) gives [].
    data = dumped.get("data") if isinstance(dumped, dict) else None
    if not isinstance(data, dict):
        return []
    tiers = [data.get("image"), *(data.get("mosaic") or [])]
    keys: list[str] = []
    for tier in tiers:
        small = tier.get("small") if isinstance(tier, dict) else None
        if isinstance(small, str) and image_key(small) not in keys:
            keys.append(image_key(small))
    return keys


def load_image_colors(db: Client, scope: ImageColorScope, payload: BaseModel) -> None:
    # This dump is the pass that collects the keys: every _tiers() call notes
    # its image in the scope. The real serialization is made by FastAPI
    # afterwards and finds the colors loaded below.
    dumped = payload.model_dump()
    main_keys = _main_image_keys(dumped)

    eligible = {key: url for key, url in scope.seen.items() if is_color_eligible(url)}
    if not eligible:
        scope.loaded = True
        return

    try:
        with translate_upstream_errors():
            response = db.rpc("get_image_colors", {"p_keys": list(eligible)}).execute()
            rows = [StoredImageColor.model_validate(row) for row in response.data]
    except (UpstreamError, UpstreamTimeout) as exc:
        logger.warning(
            "image_colors read failed keys=%d: %s",
            len(eligible),
            type(exc.__cause__).__name__,
        )
        scope.read_failed = True
        scope.loaded = True
        return

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.image_color_retry_after)
    abandoned_cutoff = now - timedelta(seconds=settings.image_color_abandoned_after)
    scope.colors = {row.image_key: row.color for row in rows if row.color is not None}
    settled = {
        row.image_key
        for row in rows
        if row.color is not None
        or row.attempted_at
        >= (cutoff if row.failed_at is not None else abandoned_cutoff)
    }
    # A row without color and without failed_at is a key a request claimed
    # and has not written: still queued or downloading, or left hanging by a
    # forced shutdown, a failed write of _compute_one, or an exception outside
    # the tuple _compute_one catches (it escapes before the write). All of
    # them count as in progress during image_color_abandoned_after; after
    # that they go to pending and are claimed again, the main image first and
    # never cut by the cap, because pending is ordered that way. A job still
    # in the queue after that time is claimed again too and can be computed
    # twice (ADR 022).
    scope.in_progress = {
        row.image_key
        for row in rows
        if row.color is None
        and row.failed_at is None
        and row.attempted_at >= abandoned_cutoff
    }
    pending = {key: url for key, url in eligible.items() if key not in settled}
    # The main images go first, in order; the rest keep the order of the
    # response.
    scope.primary = [key for key in main_keys if key in pending]
    scope.pending = {key: pending[key] for key in scope.primary}
    scope.pending.update(
        {key: url for key, url in pending.items() if key not in scope.pending}
    )
    scope.loaded = True


@lru_cache
def _http_client() -> httpx.Client:
    # No redirects, ever: a redirect would leave the allowed hosts.
    return httpx.Client(
        timeout=settings.image_color_fetch_timeout,
        follow_redirects=False,
        headers={"Accept": "image/*"},
    )


def _fetch_image(url: str) -> bytes:
    max_bytes = settings.image_color_max_bytes
    # The httpx timeout is per phase; this one is for the whole download.
    deadline = time.monotonic() + settings.image_color_fetch_timeout
    with _http_client().stream("GET", url) as response:
        if response.status_code != 200:
            raise _ImageRejected("unexpected status")
        if not response.headers.get("content-type", "").startswith("image/"):
            raise _ImageRejected("not an image")
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise _ImageRejected("declared length over the cap")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise _ImageRejected("body over the cap")
            if time.monotonic() > deadline:
                raise _ImageRejected("download took too long")
    return bytes(body)


async def compute_image_colors(db: Client, scope: ImageColorScope) -> None:
    if not scope.loaded or scope.read_failed or not scope.pending:
        return
    # The main images are never cut by the cap.
    limit = max(settings.image_color_max_per_request, len(scope.primary))
    candidates = list(scope.pending)[:limit]
    now = datetime.now(UTC)
    claimed = await asyncio.to_thread(_claim, db, candidates, now)
    # The database does not promise the order of what it returns: queue in
    # the order of the candidates, so the main image goes first.
    claimed = [key for key in candidates if key in set(claimed)]
    loop = asyncio.get_running_loop()
    await asyncio.gather(
        *(
            loop.run_in_executor(
                _pool(), _compute_one, db, key, scope.pending[key], now
            )
            for key in claimed
        )
    )


def _claim(db: Client, candidates: list[str], now: datetime) -> list[str]:
    retry_before = now - timedelta(seconds=settings.image_color_retry_after)
    abandoned_before = now - timedelta(seconds=settings.image_color_abandoned_after)
    try:
        with translate_upstream_errors():
            response = db.rpc(
                "claim_image_colors",
                {
                    "p_keys": candidates,
                    "p_attempted_at": now.isoformat(),
                    "p_retry_before": retry_before.isoformat(),
                    "p_abandoned_before": abandoned_before.isoformat(),
                },
            ).execute()
            return [row["image_key"] for row in response.data]
    except (UpstreamError, UpstreamTimeout) as exc:
        logger.warning(
            "image_color claim failed keys=%d: %s",
            len(candidates),
            type(exc.__cause__).__name__,
        )
        return []


def _compute_one(db: Client, key: str, url: str, attempted_at: datetime) -> None:
    # Runs in the pool: downloads, computes and writes the row of ONE image as
    # soon as it is done.
    background = hex_to_rgb(settings.image_color_background)
    color = None
    try:
        data = _fetch_image(url)
        rgb = dominant_rgb(data, max_pixels=settings.image_color_max_pixels)
        color = to_hex(
            adjust_for_dark_theme(
                rgb,
                background=background,
                min_background_contrast=settings.image_color_min_background_contrast,
                min_white_text_contrast=settings.image_color_min_white_text_contrast,
            )
        )
    except (
        _ImageRejected,
        httpx.HTTPError,
        # Does not inherit from HTTPError; defense in depth, the
        # eligibility guard already keeps such URLs out.
        httpx.InvalidURL,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        # How Pillow reports a PNG with broken chunks.
        SyntaxError,
    ) as exc:
        # A type outside this tuple is a bug and is left to escape on
        # purpose, to the server log.
        logger.warning(
            "image_color failed key=%s: %s", _key_digest(key), type(exc).__name__
        )
    else:
        logger.info("image_color computed key=%s color=%s", _key_digest(key), color)
    row = {
        "image_key": key,
        "color": color,
        "attempted_at": attempted_at.isoformat(),
        "failed_at": None if color else datetime.now(UTC).isoformat(),
    }
    try:
        with translate_upstream_errors():
            table = db.table("image_colors")
            if color:
                table.upsert([row], on_conflict="image_key").execute()
            else:
                # A failure never overwrites a color: a second job of the same
                # key (claimed again after image_color_abandoned_after) may
                # have stored one meanwhile. The attempted_at of the claim is
                # this job's token: if another job claimed the key again (a
                # newer claim), this failure does not touch it and the other
                # job decides. The row exists, the claim inserted it
                # (ADR 022).
                (
                    table.update(row)
                    .eq("image_key", key)
                    .is_("color", "null")
                    .eq("attempted_at", row["attempted_at"])
                    .execute()
                )
    except (UpstreamError, UpstreamTimeout) as exc:
        # The row stays without color and without failed_at: it counts as in
        # progress during image_color_abandoned_after and is claimed again
        # after that.
        logger.warning(
            "image_color write failed rows=1: %s", type(exc.__cause__).__name__
        )
