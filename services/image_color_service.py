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
#   image_colors.failed_at (D7). Its except is a fixed tuple (_ImageRejected,
#   httpx.HTTPError, httpx.InvalidURL, OSError, ValueError,
#   Image.DecompressionBombError, SyntaxError); an exception outside it is a
#   bug and escapes to the ASGI server log.
# The log lines carry the class name of the failure and a digest of the image
# key, never a message and never the URL.

import hashlib
import logging
import threading
import time
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
from core.upstream import translate_upstream_errors
from models.images import StoredImageColor

logger = logging.getLogger(__name__)

_MAX_URL_LENGTH = 2048

# At most image_color_max_concurrency computations at once per process, so
# the background tasks cannot eat the threadpool the requests share. A task
# that finds no free slot gives up: its keys are not claimed and a later
# request picks them up.
_slots = threading.BoundedSemaphore(settings.image_color_max_concurrency)


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


def load_image_colors(db: Client, scope: ImageColorScope, payload: BaseModel) -> None:
    # This dump is the pass that collects the keys: every _tiers() call notes
    # its image in the scope. The real serialization is made by FastAPI
    # afterwards and finds the colors loaded below.
    payload.model_dump()

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
    scope.colors = {row.image_key: row.color for row in rows if row.color is not None}
    settled = {
        row.image_key
        for row in rows
        if row.color is not None or row.attempted_at >= cutoff
    }
    scope.pending = {key: url for key, url in eligible.items() if key not in settled}
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


def compute_image_colors(db: Client, scope: ImageColorScope) -> None:
    if not scope.loaded or scope.read_failed or not scope.pending:
        return
    if not _slots.acquire(blocking=False):
        logger.debug("image_color skipped: no free slot")
        return
    try:
        _compute_claimed(db, scope)
    finally:
        _slots.release()


def _compute_claimed(db: Client, scope: ImageColorScope) -> None:
    candidates = list(scope.pending)[: settings.image_color_max_per_request]
    now = datetime.now(UTC)
    retry_before = now - timedelta(seconds=settings.image_color_retry_after)
    try:
        with translate_upstream_errors():
            response = db.rpc(
                "claim_image_colors",
                {
                    "p_keys": candidates,
                    "p_attempted_at": now.isoformat(),
                    "p_retry_before": retry_before.isoformat(),
                },
            ).execute()
            claimed = [row["image_key"] for row in response.data]
    except (UpstreamError, UpstreamTimeout) as exc:
        logger.warning(
            "image_color claim failed keys=%d: %s",
            len(candidates),
            type(exc.__cause__).__name__,
        )
        return

    background = hex_to_rgb(settings.image_color_background)
    rows = []
    for key in claimed:
        color = None
        try:
            data = _fetch_image(scope.pending[key])
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
        rows.append(
            {
                "image_key": key,
                "color": color,
                "attempted_at": now.isoformat(),
                "failed_at": None if color else datetime.now(UTC).isoformat(),
            }
        )

    if not rows:
        return
    try:
        with translate_upstream_errors():
            db.table("image_colors").upsert(rows, on_conflict="image_key").execute()
    except (UpstreamError, UpstreamTimeout) as exc:
        # The rows stay claimed and are retried after the retry window.
        logger.warning(
            "image_color write failed rows=%d: %s",
            len(rows),
            type(exc.__cause__).__name__,
        )
