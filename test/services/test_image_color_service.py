# test/services/test_image_color_service.py
#
# Tests for the image color service: which URLs may be read and downloaded,
# the one batch read of stored colors, and the computation that runs after
# the response.
#
# Tested:
# - Only https URLs on an allowed exact hostname, with no userinfo, the
#   default port and at most 2048 characters, are eligible (169.254.169.254
#   and localhost are not)
# - A malformed URL (bad port, bad IPv6, control characters) is not eligible
#   and never raises; with no allowed hosts nothing is eligible and the
#   startup WARNING is logged
# - A SyntaxError from the decoder and an httpx.InvalidURL fail one key, not
#   the batch
# - An ineligible URL is never read, claimed or downloaded
# - A stored color is served without claiming or downloading
# - A stored color with the wrong format is a failed read: the scope carries
#   no color and the computation does nothing
# - A read failure (database down) keeps color null, loads the scope and
#   claims nothing; the load itself never downloads
# - Two sizes of one image are one key, one claim and one download, and the
#   stored color is the adjusted dominant color
# - A key claimed by another request is not downloaded
# - A key being computed (no color, no failed_at, inside the window) is
#   awaited and not claimed or downloaded again, main or not; a row without
#   color and without failed_at older than the window is claimed again
# - A failed download is stored as failed with color null; a recent failure
#   is not claimed again, an older one is
# - A claim without color and without failed_at older than
#   image_color_abandoned_after is pending, claimed again with the main image
#   first and computed; at 29 s it is still in progress; a failure of that age
#   is not claimed again
# - A failure is written with an update conditioned on the row having no
#   color and on still being the job's own claim, so a late failure never
#   overwrites a stored color nor marks as failed a key claimed again
# - A redirect is never followed, a non-image content type, a declared length
#   or a body over the cap, and a timeout all fail without a color
# - A failed claim and a failed write (of a color or of a failure) are logged
#   and skipped without raising
# - At most image_color_max_per_request keys are claimed, except the main
#   images (the top-level image, the mosaic tiles), which go first in
#   scope.pending and are never cut; a payload with none keeps its order
# - Claimed keys are queued in the order of the candidates
# - At most image_color_max_parallel_fetches downloads run at once
# - Each color is written with its own upsert as soon as it is computed, and
#   the failed write of one image does not block the others
# - Two batches at once both claim and finish: nothing is dropped
# - compute_image_colors is a coroutine function
# - Every key sent to the database (the read, the claim, the write of a
#   color, the write of a failure) carries the algorithm version prefix and
#   the scope keeps the bare key; a row without the prefix is ignored,
#   claimed again and left untouched; a row or a claim of a key not asked
#   for is ignored; the log digest is of the bare key (ADR 023)
#
# What is covered:
# - services/image_color_service.py, with FakeImageColorDb (test/conftest.py)
#   and httpx.MockTransport for the download
#
# Run with: pytest test/services/test_image_color_service.py -v
#
# SEE: services/image_color_service.py, test/conftest.py

import asyncio
import contextvars
import hashlib
import inspect
import threading
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any

import httpx
import pytest
from PIL import Image
from pydantic import BaseModel, computed_field

from core.config import settings
from core.image_color_scope import open_image_color_scope
from core.thumbnails import image_tiers, mosaic_tiers
from models.images import ImageTiers
from services import image_color_service
from services.image_color_service import (
    _stored_key,
    compute_image_colors,
    is_color_eligible,
    load_image_colors,
)

# The module attribute is replaced by a stub for every test (test/conftest.py);
# the real function is kept from collection time.
_real_fetch_image = image_color_service._fetch_image


class _Album(BaseModel):
    # Serializes the related items BEFORE the computed image, like Album.
    related: list[_Cover]
    cover_url: str

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.cover_url)


class _Playlist(BaseModel):
    urls: list[str]

    @computed_field
    @property
    def mosaic(self) -> list[ImageTiers]:
        return mosaic_tiers(self.urls)


class _Envelope(BaseModel):
    data: Any


def _load_payload(db, payload):
    def run():
        scope = open_image_color_scope()
        load_image_colors(db, scope, payload)
        return scope

    return contextvars.copy_context().run(run)


def _compute(db, scope):
    asyncio.run(compute_image_colors(db, scope))


@pytest.fixture(autouse=True)
def _fresh_pool():
    # The pool is sized from the settings when first used: every test builds
    # its own and leaves none behind.
    image_color_service._pool.cache_clear()
    yield
    if image_color_service._pool.cache_info().currsize:
        image_color_service._pool().shutdown(wait=True)
    image_color_service._pool.cache_clear()


_HOST = "https://lh3.googleusercontent.com/abc"
_KEY = _HOST


class _Cover(BaseModel):
    url: str

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.url)


class _Covers(BaseModel):
    items: list[_Cover]


def _red_png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (192, 192), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def _load(db, *urls):
    def run():
        scope = open_image_color_scope()
        load_image_colors(db, scope, _Covers(items=[_Cover(url=u) for u in urls]))
        return scope

    return contextvars.copy_context().run(run)


# --- Eligibility (the SSRF guard) -------------------------------------------


def test_only_https_urls_are_eligible():
    assert is_color_eligible(f"{_HOST}=w192-h192-p")
    assert not is_color_eligible("http://lh3.googleusercontent.com/abc")
    assert not is_color_eligible("ftp://lh3.googleusercontent.com/abc")
    assert not is_color_eligible("//lh3.googleusercontent.com/abc")


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/abc",
        "https://127.0.0.1/abc",
        "https://example.com/abc",
        "https://lh3.googleusercontent.com.evil.example/abc",
        "https://evil.example/lh3.googleusercontent.com",
    ],
)
def test_only_allowed_hosts_are_eligible(url):
    assert not is_color_eligible(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://lh3.googleusercontent.com:99999/cover",
        "https://lh3.googleusercontent.com:abc/cover",
        "https://[abc/cover",
        "https://lh3.googleusercontent.com/a\x00b",
        "https://lh3.googleusercontent.com/a\x01b",
        "https://lh3.googleusercontent.com/a\x1bb",
        "https://lh3.googleusercontent.com/a\x7fb",
        "https://lh3.googleusercontent.com/a\tb",
        "https://lh3.googleusercontent.com/a\nb",
    ],
)
def test_malformed_urls_are_not_eligible_and_never_raise(url, monkeypatch):
    monkeypatch.setattr(
        settings, "image_color_allowed_hosts", ["lh3.googleusercontent.com"]
    )

    assert is_color_eligible(url) is False


def test_nothing_is_eligible_with_no_allowed_hosts(monkeypatch):
    monkeypatch.setattr(settings, "image_color_allowed_hosts", [])

    assert not is_color_eligible(_HOST)
    assert not is_color_eligible("https://example.com/abc")


def test_the_startup_warns_once_when_there_are_no_allowed_hosts(monkeypatch, caplog):
    from app import warn_if_no_image_color_hosts

    monkeypatch.setattr(settings, "image_color_allowed_hosts", [])
    with caplog.at_level("WARNING", logger="app"):
        warn_if_no_image_color_hosts()
    assert [r.getMessage() for r in caplog.records if r.name == "app"] == [
        "image_color_allowed_hosts is empty: no image colors will be computed"
    ]

    caplog.clear()
    monkeypatch.setattr(settings, "image_color_allowed_hosts", ["a.example.com"])
    with caplog.at_level("WARNING", logger="app"):
        warn_if_no_image_color_hosts()
    assert caplog.records == []


def test_the_hostname_match_ignores_case():
    assert is_color_eligible("https://LH3.GoogleUserContent.com/abc")


@pytest.mark.parametrize(
    "url",
    [
        "https://user@lh3.googleusercontent.com/abc",
        "https://user:pass@lh3.googleusercontent.com/abc",
        "https://lh3.googleusercontent.com:8443/abc",
    ],
)
def test_userinfo_and_ports_are_not_eligible(url):
    assert not is_color_eligible(url)


def test_the_default_port_is_eligible():
    assert is_color_eligible("https://lh3.googleusercontent.com:443/abc")


def test_overlong_urls_are_not_eligible():
    assert not is_color_eligible(f"{_HOST}/{'a' * 2048}")


def test_an_ineligible_url_is_never_read_claimed_or_downloaded(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, "https://169.254.169.254/x=w60-h60")
    _compute(image_color_db, scope)

    assert scope.loaded
    assert image_color_db.reads == []
    assert image_color_db.claims == []
    assert stub.calls == []


# --- Reading ---------------------------------------------------------------


def test_a_stored_color_is_served_without_claiming_or_downloading(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.store(_stored_key(_KEY), "#123456")

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert scope.colors == {_KEY: "#123456"}
    assert scope.pending == {}
    assert image_color_db.reads == [[_stored_key(_KEY)]]
    assert image_color_db.claims == []
    assert stub.calls == []


def test_a_stored_color_with_the_wrong_format_is_a_read_failure(image_color_db):
    image_color_db.store(_stored_key(_KEY), "#FF0000")

    scope = _load(image_color_db, f"{_HOST}=w60-h60")

    assert scope.read_failed
    assert scope.loaded
    assert scope.colors == {}
    _compute(image_color_db, scope)
    assert image_color_db.claims == []


def test_a_database_failure_keeps_color_null_and_claims_nothing(image_color_db, caplog):
    image_color_db.read_error = httpx.ConnectError("database down")

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert scope.read_failed
    assert scope.loaded
    assert scope.colors == {}
    assert scope.in_progress == set()
    assert image_color_db.claims == []
    assert "image_colors read failed" in caplog.text
    assert "database down" not in caplog.text


def test_load_never_downloads(image_color_db, allow_image_downloads):
    stub = allow_image_downloads(lambda url: _red_png())

    scope = _load(image_color_db, f"{_HOST}=w60-h60")

    assert scope.pending == {_KEY: f"{_HOST}=w192-h192-p"}
    assert stub.calls == []


# --- Computing -------------------------------------------------------------


def test_two_sizes_of_one_image_are_one_key_one_claim_one_download(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, f"{_HOST}=w60-h60", f"{_HOST}=w544-h544-p")
    _compute(image_color_db, scope)

    assert image_color_db.reads == [[_stored_key(_KEY)]]
    assert image_color_db.claims == [[_stored_key(_KEY)]]
    assert stub.calls == [f"{_HOST}=w192-h192-p"]
    assert len(image_color_db.writes) == 1
    row = image_color_db.writes[0]
    assert row["image_key"] == _stored_key(_KEY)
    assert row["color"] == "#ee0000"
    assert row["failed_at"] is None


def test_a_key_claimed_by_another_request_is_not_downloaded(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert image_color_db.claims == [[_stored_key(_KEY)]]
    assert stub.calls == []
    assert image_color_db.writes == []


def test_a_failed_download_is_stored_as_failed_with_null_color(
    image_color_db, allow_image_downloads
):
    def fail(url):
        raise httpx.ConnectError("unreachable")

    allow_image_downloads(fail)
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    row = image_color_db.stored[_stored_key(_KEY)]
    assert row["color"] is None
    assert row["failed_at"] is not None


def test_bytes_that_are_not_an_image_are_stored_as_failed(
    image_color_db, allow_image_downloads
):
    allow_image_downloads(lambda url: b"not an image")
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert image_color_db.stored[_stored_key(_KEY)]["color"] is None
    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


def test_a_syntax_error_from_the_decoder_fails_one_key_not_the_batch(
    image_color_db, allow_image_downloads, monkeypatch
):
    # Pillow reports a PNG with broken chunks as a SyntaxError.
    real = image_color_service.dominant_rgb

    def decode(data, max_pixels):
        if data == b"broken":
            raise SyntaxError("broken PNG file")
        return real(data, max_pixels=max_pixels)

    monkeypatch.setattr(image_color_service, "dominant_rgb", decode)
    allow_image_downloads(lambda url: b"broken" if "bad" in url else _red_png())
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, f"{_HOST}-bad=w60-h60", f"{_HOST}-good=w60-h60")
    _compute(image_color_db, scope)

    assert len(image_color_db.writes) == 2
    assert image_color_db.stored[_stored_key(f"{_HOST}-bad")]["color"] is None
    assert image_color_db.stored[_stored_key(f"{_HOST}-bad")]["failed_at"] is not None
    assert image_color_db.stored[_stored_key(f"{_HOST}-good")]["color"] == "#ee0000"


def test_an_invalid_url_error_fails_one_key_not_the_batch(
    image_color_db, allow_image_downloads
):
    def fetch(url):
        if "bad" in url:
            raise httpx.InvalidURL("invalid URL")
        return _red_png()

    allow_image_downloads(fetch)
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, f"{_HOST}-bad=w60-h60", f"{_HOST}-good=w60-h60")
    _compute(image_color_db, scope)

    assert len(image_color_db.writes) == 2
    assert image_color_db.stored[_stored_key(f"{_HOST}-bad")]["color"] is None
    assert image_color_db.stored[_stored_key(f"{_HOST}-bad")]["failed_at"] is not None
    assert image_color_db.stored[_stored_key(f"{_HOST}-good")]["color"] == "#ee0000"


def test_the_real_fetch_raises_invalid_url_for_a_control_character(monkeypatch):
    # Why InvalidURL is in the tuple: it is not an httpx.HTTPError.
    _use_transport(monkeypatch, lambda request: httpx.Response(200))

    with pytest.raises(httpx.InvalidURL):
        _real_fetch_image("https://lh3.googleusercontent.com/\x01a")


def test_a_recent_failure_is_not_claimed_again(image_color_db):
    image_color_db.store(_stored_key(_KEY), None, failed=True)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert scope.pending == {}
    assert scope.in_progress == set()
    assert not scope.awaiting_colors
    assert image_color_db.claims == []


def test_a_key_being_computed_is_awaited_and_not_claimed_again(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.store(_stored_key(_KEY), None)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert scope.pending == {}
    assert scope.in_progress == {_KEY}
    assert scope.awaiting_colors
    assert image_color_db.claims == []
    assert stub.calls == []


def test_a_related_key_being_computed_also_awaits(image_color_db):
    image_color_db.store(_stored_key(f"{_HOST}cover"), "#123456")
    image_color_db.store(_stored_key(f"{_HOST}r0"), None)

    scope = _load_payload(image_color_db, _album_envelope(1))

    assert scope.primary == []
    assert scope.pending == {}
    assert scope.in_progress == {f"{_HOST}r0"}
    assert scope.awaiting_colors


def test_a_claim_left_hanging_is_claimed_again_after_the_window(
    image_color_db, allow_image_downloads
):
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    old = datetime.now(UTC) - timedelta(seconds=settings.image_color_retry_after + 60)
    image_color_db.store(_stored_key(_KEY), None, attempted_at=old)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert _KEY in scope.pending
    assert scope.in_progress == set()
    assert image_color_db.claims == [[_stored_key(_KEY)]]
    assert image_color_db.stored[_stored_key(_KEY)]["color"] == "#ee0000"


def test_a_failure_older_than_the_retry_window_is_claimed_again(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    old = datetime.now(UTC) - timedelta(seconds=settings.image_color_retry_after + 60)
    image_color_db.store(_stored_key(_KEY), None, attempted_at=old, failed=True)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert image_color_db.claims == [[_stored_key(_KEY)]]
    assert stub.calls == [f"{_HOST}=w192-h192-p"]
    assert image_color_db.stored[_stored_key(_KEY)]["color"] == "#ee0000"
    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is None


def _stored_ago(db, seconds, *, failed=False):
    db.store(
        _stored_key(_KEY),
        None,
        attempted_at=datetime.now(UTC) - timedelta(seconds=seconds),
        failed=failed,
    )


def test_an_abandoned_claim_is_claimed_again_and_computed(
    image_color_db, allow_image_downloads
):
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    _stored_ago(image_color_db, settings.image_color_abandoned_after + 1)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")

    assert _KEY in scope.pending
    assert scope.in_progress == set()
    assert scope.awaiting_colors
    _compute(image_color_db, scope)

    assert image_color_db.claims == [[_stored_key(_KEY)]]
    assert image_color_db.stored[_stored_key(_KEY)]["color"] == "#ee0000"
    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is None
    params = image_color_db.claim_params[0]
    assert datetime.fromisoformat(params["p_attempted_at"]) - datetime.fromisoformat(
        params["p_abandoned_before"]
    ) == timedelta(seconds=settings.image_color_abandoned_after)


def test_a_claim_just_under_the_threshold_is_still_in_progress(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    _stored_ago(image_color_db, settings.image_color_abandoned_after - 1)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")

    assert scope.in_progress == {_KEY}
    assert scope.pending == {}
    assert scope.awaiting_colors
    _compute(image_color_db, scope)

    assert image_color_db.claims == []
    assert stub.calls == []


def test_a_failure_older_than_the_threshold_is_still_not_claimed_again(
    image_color_db,
):
    image_color_db.claims_like_the_database = True
    _stored_ago(image_color_db, settings.image_color_abandoned_after + 1, failed=True)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert scope.pending == {}
    assert scope.in_progress == set()
    assert not scope.awaiting_colors
    assert image_color_db.claims == []


def test_an_abandoned_cover_goes_first_and_is_never_cut(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_per_request", 10)
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    image_color_db.store(
        _stored_key(f"{_HOST}cover"),
        None,
        attempted_at=datetime.now(UTC)
        - timedelta(seconds=settings.image_color_abandoned_after + 1),
    )
    scope = _load_payload(image_color_db, _album_envelope(11))

    _compute(image_color_db, scope)

    assert scope.primary == [f"{_HOST}cover"]
    assert image_color_db.claims[0][0] == _stored_key(f"{_HOST}cover")
    assert len(image_color_db.claims[0]) == 10


def test_a_late_failure_never_overwrites_a_stored_color(
    image_color_db, allow_image_downloads
):
    # Two jobs of one key (the first was still queued after the threshold and
    # the key was claimed again): the first stores the color while the second
    # downloads, and the failure of the second must leave it alone.
    def fetch(url):
        image_color_db.store(_stored_key(_KEY), "#ee0000")
        raise httpx.ConnectError("unreachable")

    allow_image_downloads(fetch)
    image_color_db.claims_like_the_database = True
    _stored_ago(image_color_db, settings.image_color_abandoned_after + 1)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert len(image_color_db.update_calls) == 1
    assert image_color_db.update_calls[0]["failed_at"] is not None
    assert image_color_db.stored[_stored_key(_KEY)]["color"] == "#ee0000"
    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is None


def test_a_late_failure_never_marks_a_claim_taken_again_as_failed(
    image_color_db, allow_image_downloads
):
    reclaimed = datetime.now(UTC).isoformat()

    def fetch(url):
        # Another job claims the key again while this one downloads.
        image_color_db.stored[_stored_key(_KEY)]["attempted_at"] = reclaimed
        raise httpx.ConnectError("unreachable")

    allow_image_downloads(fetch)
    image_color_db.claims_like_the_database = True
    _stored_ago(image_color_db, settings.image_color_abandoned_after + 1)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert len(image_color_db.update_calls) == 1
    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is None
    assert image_color_db.stored[_stored_key(_KEY)]["attempted_at"] == reclaimed


def test_a_failure_is_written_with_an_update_conditioned_on_no_color(
    image_color_db, allow_image_downloads
):
    def fail(url):
        raise httpx.ConnectError("unreachable")

    allow_image_downloads(fail)
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    _compute(image_color_db, scope)

    assert image_color_db.upsert_calls == []
    assert len(image_color_db.update_calls) == 1
    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


def test_at_most_max_per_request_keys_are_claimed(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_per_request", 2)
    allow_image_downloads(lambda url: _red_png())
    urls = [f"{_HOST}{n}=w60-h60" for n in range(5)]

    scope = _load(image_color_db, *urls)
    _compute(image_color_db, scope)

    assert len(scope.pending) == 5
    assert image_color_db.claims == [
        [_stored_key(f"{_HOST}0"), _stored_key(f"{_HOST}1")]
    ]


def test_a_failed_claim_is_logged_and_nothing_is_downloaded(
    image_color_db, allow_image_downloads, caplog
):
    stub = allow_image_downloads(lambda url: _red_png())
    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    image_color_db.claim_error = httpx.ConnectError("database down")

    _compute(image_color_db, scope)

    assert stub.calls == []
    assert "image_color claim failed" in caplog.text


def test_a_failed_write_is_logged_and_does_not_raise(
    image_color_db, allow_image_downloads, caplog
):
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    image_color_db.write_error = httpx.ConnectError("database down")

    _compute(image_color_db, scope)

    assert "image_color write failed" in caplog.text


def test_a_failed_write_of_a_failure_is_logged_and_does_not_raise(
    image_color_db, allow_image_downloads, caplog
):
    def fail(url):
        raise httpx.ConnectError("unreachable")

    allow_image_downloads(fail)
    image_color_db.claims_like_the_database = True
    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    image_color_db.write_error_for = {_stored_key(_KEY)}

    _compute(image_color_db, scope)

    assert len(image_color_db.update_calls) == 1
    assert image_color_db.upsert_calls == []
    assert "image_color write failed" in caplog.text
    assert "database down" not in caplog.text


# --- The download ----------------------------------------------------------


class _ExplodingStream(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError("the body must not be read")


def _use_transport(monkeypatch, handler):
    requests = []

    def recording(request):
        requests.append(request)
        return handler(request)

    client = httpx.Client(
        transport=httpx.MockTransport(recording), follow_redirects=False
    )
    monkeypatch.setattr(image_color_service, "_http_client", lambda: client)
    monkeypatch.setattr(image_color_service, "_fetch_image", _real_fetch_image)
    return requests


def _compute_with_real_fetch(db):
    db.claims_like_the_database = True
    scope = _load(db, f"{_HOST}=w60-h60")
    _compute(db, scope)
    return scope


def test_a_successful_download_is_decoded_and_stored(image_color_db, monkeypatch):
    requests = _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "image/png"}, content=_red_png()
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert [str(r.url) for r in requests] == [f"{_HOST}=w192-h192-p"]
    assert image_color_db.stored[_stored_key(_KEY)]["color"] == "#ee0000"


def test_a_redirect_is_never_followed(image_color_db, monkeypatch):
    requests = _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://169.254.169.254/latest"}
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert len(requests) == 1
    assert image_color_db.stored[_stored_key(_KEY)]["color"] is None
    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


def test_a_non_image_content_type_fails(image_color_db, monkeypatch):
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=_red_png()
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


def test_a_non_200_status_fails(image_color_db, monkeypatch):
    _use_transport(monkeypatch, lambda request: httpx.Response(404))

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


def test_a_declared_length_over_the_cap_fails_without_reading_the_body(
    image_color_db, monkeypatch
):
    monkeypatch.setattr(settings, "image_color_max_bytes", 100)
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "101"},
            stream=_ExplodingStream(),
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


def test_a_body_over_the_cap_fails(image_color_db, monkeypatch):
    monkeypatch.setattr(settings, "image_color_max_bytes", 100)

    def chunks():
        for _ in range(10):
            yield b"x" * 30

    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "image/png"}, content=chunks()
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


def test_a_timeout_fails(image_color_db, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    _use_transport(monkeypatch, timeout)

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_stored_key(_KEY)]["color"] is None
    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


def test_an_image_over_the_pixel_cap_fails(image_color_db, monkeypatch):
    monkeypatch.setattr(settings, "image_color_max_pixels", 100)
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "image/png"}, content=_red_png()
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_stored_key(_KEY)]["failed_at"] is not None


# --- Main image first, parallel computation, one write per image -------------


def _album_envelope(n_related: int) -> _Envelope:
    related = [_Cover(url=f"{_HOST}r{n}=w60-h60") for n in range(n_related)]
    return _Envelope(data=_Album(related=related, cover_url=f"{_HOST}cover=w60-h60"))


def test_the_main_image_is_pending_first(image_color_db):
    scope = _load_payload(image_color_db, _album_envelope(3))

    assert next(iter(scope.pending)) == f"{_HOST}cover"
    assert scope.primary == [f"{_HOST}cover"]
    assert len(scope.pending) == 4


def test_the_cover_is_claimed_with_twelve_new_images_and_a_cap_of_ten(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_per_request", 10)
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    scope = _load_payload(image_color_db, _album_envelope(11))

    _compute(image_color_db, scope)

    assert len(image_color_db.claims) == 1
    assert len(image_color_db.claims[0]) == 10
    assert image_color_db.claims[0][0] == _stored_key(f"{_HOST}cover")


def test_every_mosaic_tile_is_claimed_even_over_the_cap(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_per_request", 1)
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    urls = [f"{_HOST}t{n}=w60-h60" for n in range(4)]
    scope = _load_payload(image_color_db, _Envelope(data=_Playlist(urls=urls)))

    _compute(image_color_db, scope)

    assert len(scope.primary) == 4
    assert len(image_color_db.claims[0]) == 4


def test_search_keeps_its_order(image_color_db):
    class _Result(BaseModel):
        songs: list[_Cover]
        albums: list[_Cover]

    payload = _Envelope(
        data=_Result(
            songs=[_Cover(url=f"{_HOST}s{n}=w60-h60") for n in range(2)],
            albums=[_Cover(url=f"{_HOST}a{n}=w60-h60") for n in range(2)],
        )
    )

    scope = _load_payload(image_color_db, payload)

    assert list(scope.pending) == [f"{_HOST}{k}" for k in ("s0", "s1", "a0", "a1")]
    assert scope.primary == []


def test_claimed_keys_are_queued_in_candidate_order(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_parallel_fetches", 1)
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    original = image_color_db._claim
    monkeypatch.setattr(
        image_color_db, "_claim", lambda params: list(reversed(original(params)))
    )
    scope = _load_payload(image_color_db, _album_envelope(3))

    _compute(image_color_db, scope)

    assert stub.calls[0] == f"{_HOST}cover=w192-h192-p"
    assert len(stub.calls) == 4


def test_at_most_max_parallel_fetches_run_at_once(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_parallel_fetches", 2)
    image_color_db.claims_like_the_database = True
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=2)
    state = {"active": 0, "max": 0}

    def fetch(url):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        finally:
            with lock:
                state["active"] -= 1
        return _red_png()

    allow_image_downloads(fetch)
    scope = _load(image_color_db, *[f"{_HOST}{n}=w60-h60" for n in range(6)])

    _compute(image_color_db, scope)

    assert state["max"] == 2
    assert len(image_color_db.stored) == 6
    assert not barrier.broken


def test_the_first_color_to_finish_is_written_before_the_others_finish(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_parallel_fetches", 4)
    image_color_db.claims_like_the_database = True
    release = threading.Event()
    inside = threading.Semaphore(0)

    def fetch(url):
        if f"{_HOST}a=" in url:
            return _red_png()
        inside.release()
        assert release.wait(timeout=5)
        return _red_png()

    allow_image_downloads(fetch)
    scope = _load(
        image_color_db, f"{_HOST}a=w60-h60", f"{_HOST}b=w60-h60", f"{_HOST}c=w60-h60"
    )
    thread = threading.Thread(target=_compute, args=(image_color_db, scope))
    thread.start()
    try:
        assert image_color_db.written.wait(timeout=2)
        assert inside.acquire(timeout=2)
        assert inside.acquire(timeout=2)
        with image_color_db.lock:
            colors = {k: v["color"] for k, v in image_color_db.stored.items()}
        assert colors[_stored_key(f"{_HOST}a")] == "#ee0000"
        assert colors[_stored_key(f"{_HOST}b")] is None
        assert colors[_stored_key(f"{_HOST}c")] is None
    finally:
        release.set()
        thread.join(timeout=5)

    assert all(row["color"] == "#ee0000" for row in image_color_db.stored.values())
    assert len(image_color_db.stored) == 3


def test_each_color_is_written_with_its_own_upsert(
    image_color_db, allow_image_downloads
):
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    scope = _load(image_color_db, *[f"{_HOST}{n}=w60-h60" for n in range(3)])

    _compute(image_color_db, scope)

    assert len(image_color_db.upsert_calls) == 3
    assert all(len(rows) == 1 for rows in image_color_db.upsert_calls)


def test_a_failed_write_of_one_image_does_not_block_the_others(
    image_color_db, allow_image_downloads, caplog
):
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    image_color_db.write_error_for = {_stored_key(f"{_HOST}b")}
    scope = _load(
        image_color_db, f"{_HOST}a=w60-h60", f"{_HOST}b=w60-h60", f"{_HOST}c=w60-h60"
    )

    _compute(image_color_db, scope)

    assert image_color_db.stored[_stored_key(f"{_HOST}a")]["color"] == "#ee0000"
    assert image_color_db.stored[_stored_key(f"{_HOST}c")]["color"] == "#ee0000"
    # The claim row of b stays (color null): the write of its color failed.
    assert image_color_db.stored[_stored_key(f"{_HOST}b")]["color"] is None
    assert caplog.text.count("image_color write failed") == 1
    assert "database down" not in caplog.text


def test_two_batches_at_once_both_claim_their_keys(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_parallel_fetches", 1)
    image_color_db.claims_like_the_database = True
    release = threading.Event()
    started = threading.Event()

    def fetch(url):
        started.set()
        assert release.wait(timeout=5)
        return _red_png()

    allow_image_downloads(fetch)
    first = _load(image_color_db, f"{_HOST}a=w60-h60", f"{_HOST}b=w60-h60")
    second = _load(image_color_db, f"{_HOST}c=w60-h60", f"{_HOST}d=w60-h60")
    threads = [
        threading.Thread(target=_compute, args=(image_color_db, scope))
        for scope in (first, second)
    ]
    for thread in threads:
        thread.start()
    try:
        assert started.wait(timeout=2)
        deadline = datetime.now(UTC) + timedelta(seconds=2)
        while len(image_color_db.claims) < 2 and datetime.now(UTC) < deadline:
            threading.Event().wait(0.01)
        assert len(image_color_db.claims) == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)

    assert len(image_color_db.stored) == 4
    assert all(row["color"] == "#ee0000" for row in image_color_db.stored.values())


def test_the_computation_is_a_coroutine_function():
    assert inspect.iscoroutinefunction(compute_image_colors)


# --- The stored key carries the algorithm version (ADR 023) -----------------


def test_every_key_sent_to_the_database_carries_the_algorithm_version(
    image_color_db, allow_image_downloads
):
    good, bad = f"{_HOST}-good", f"{_HOST}-bad"
    allow_image_downloads(lambda url: b"" if "bad" in url else _red_png())
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, f"{good}=w60-h60", f"{bad}=w60-h60")
    _compute(image_color_db, scope)

    assert image_color_db.reads[0] == [_stored_key(good), _stored_key(bad)]
    assert image_color_db.claims[0] == [_stored_key(good), _stored_key(bad)]
    assert image_color_db.upsert_calls[0][0]["image_key"] == _stored_key(good)
    assert image_color_db.update_calls[0]["image_key"] == _stored_key(bad)
    assert image_color_db.stored[_stored_key(bad)]["failed_at"] is not None
    assert list(scope.pending) == [good, bad]
    assert all(key.startswith("v2:") for key in image_color_db.stored)


def test_an_unversioned_row_is_ignored_claimed_again_and_left_untouched(
    image_color_db, allow_image_downloads
):
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    image_color_db.store(_KEY, "#123456")
    old_row = dict(image_color_db.stored[_KEY])

    scope = _load(image_color_db, f"{_HOST}=w60-h60")

    assert scope.colors == {}
    assert _KEY in scope.pending

    _compute(image_color_db, scope)

    assert image_color_db.claims == [[_stored_key(_KEY)]]
    assert image_color_db.stored[_stored_key(_KEY)]["color"] == "#ee0000"
    assert image_color_db.stored[_KEY] == old_row


def test_a_row_for_a_key_not_asked_for_is_ignored(image_color_db, monkeypatch):
    image_color_db.store(_stored_key(_KEY), "#123456")
    real = image_color_db._read

    def read(params):
        extra = {
            "image_key": f"{_HOST}-other",
            "color": "#654321",
            "attempted_at": datetime.now(UTC).isoformat(),
            "failed_at": None,
        }
        return [*real(params), extra]

    monkeypatch.setattr(image_color_db, "_read", read)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")

    assert scope.colors == {_KEY: "#123456"}
    assert scope.read_failed is False


def test_a_claim_row_for_a_key_not_asked_for_is_ignored(
    image_color_db, allow_image_downloads, monkeypatch
):
    stub = allow_image_downloads(lambda url: _red_png())
    monkeypatch.setattr(
        image_color_db,
        "_claim",
        lambda params: [
            {"image_key": _stored_key(_KEY)},
            {"image_key": f"{_HOST}-other"},
        ],
    )
    scope = _load(image_color_db, f"{_HOST}=w60-h60")

    _compute(image_color_db, scope)

    assert stub.calls == [f"{_HOST}=w192-h192-p"]


def test_the_log_digest_is_of_the_unversioned_key(
    image_color_db, allow_image_downloads, caplog
):
    digest = hashlib.sha256(_KEY.encode()).hexdigest()[:12]
    image_color_db.claims_like_the_database = True
    caplog.set_level("INFO", logger="services.image_color_service")

    allow_image_downloads(lambda url: _red_png())
    _compute(image_color_db, _load(image_color_db, f"{_HOST}=w60-h60"))
    assert f"image_color computed key={digest} color=#ee0000" in caplog.text

    def unreachable(url):
        raise httpx.ConnectError("unreachable")

    other = f"{_HOST}-down"
    other_digest = hashlib.sha256(other.encode()).hexdigest()[:12]
    allow_image_downloads(unreachable)
    _compute(image_color_db, _load(image_color_db, f"{other}=w60-h60"))
    assert f"image_color failed key={other_digest}: ConnectError" in caplog.text
