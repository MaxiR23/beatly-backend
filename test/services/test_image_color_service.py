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
# - A failed download is stored as failed with color null; a recent failure
#   is not claimed again, an older one is
# - A redirect is never followed, a non-image content type, a declared length
#   or a body over the cap, and a timeout all fail without a color
# - No free slot, a failed claim and a failed write are logged and skipped
#   without raising
# - At most image_color_max_per_request keys are claimed
#
# What is covered:
# - services/image_color_service.py, with FakeImageColorDb (test/conftest.py)
#   and httpx.MockTransport for the download
#
# Run with: pytest test/services/test_image_color_service.py -v
#
# SEE: services/image_color_service.py, test/conftest.py

import contextvars
from datetime import UTC, datetime, timedelta
from io import BytesIO

import httpx
import pytest
from PIL import Image
from pydantic import BaseModel, computed_field

from core.config import settings
from core.image_color_scope import open_image_color_scope
from core.thumbnails import image_tiers
from models.images import ImageTiers
from services import image_color_service
from services.image_color_service import (
    compute_image_colors,
    is_color_eligible,
    load_image_colors,
)

# The module attribute is replaced by a stub for every test (test/conftest.py);
# the real function is kept from collection time.
_real_fetch_image = image_color_service._fetch_image

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
    compute_image_colors(image_color_db, scope)

    assert scope.loaded
    assert image_color_db.reads == []
    assert image_color_db.claims == []
    assert stub.calls == []


# --- Reading ---------------------------------------------------------------


def test_a_stored_color_is_served_without_claiming_or_downloading(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.store(_KEY, "#123456")

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    compute_image_colors(image_color_db, scope)

    assert scope.colors == {_KEY: "#123456"}
    assert scope.pending == {}
    assert image_color_db.reads == [[_KEY]]
    assert image_color_db.claims == []
    assert stub.calls == []


def test_a_stored_color_with_the_wrong_format_is_a_read_failure(image_color_db):
    image_color_db.store(_KEY, "#FF0000")

    scope = _load(image_color_db, f"{_HOST}=w60-h60")

    assert scope.read_failed
    assert scope.loaded
    assert scope.colors == {}
    compute_image_colors(image_color_db, scope)
    assert image_color_db.claims == []


def test_a_database_failure_keeps_color_null_and_claims_nothing(image_color_db, caplog):
    image_color_db.read_error = httpx.ConnectError("database down")

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    compute_image_colors(image_color_db, scope)

    assert scope.read_failed
    assert scope.loaded
    assert scope.colors == {}
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
    compute_image_colors(image_color_db, scope)

    assert image_color_db.reads == [[_KEY]]
    assert image_color_db.claims == [[_KEY]]
    assert stub.calls == [f"{_HOST}=w192-h192-p"]
    assert len(image_color_db.writes) == 1
    row = image_color_db.writes[0]
    assert row["image_key"] == _KEY
    assert row["color"] == "#ee0000"
    assert row["failed_at"] is None


def test_a_key_claimed_by_another_request_is_not_downloaded(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    compute_image_colors(image_color_db, scope)

    assert image_color_db.claims == [[_KEY]]
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
    compute_image_colors(image_color_db, scope)

    row = image_color_db.stored[_KEY]
    assert row["color"] is None
    assert row["failed_at"] is not None


def test_bytes_that_are_not_an_image_are_stored_as_failed(
    image_color_db, allow_image_downloads
):
    allow_image_downloads(lambda url: b"not an image")
    image_color_db.claims_like_the_database = True

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    compute_image_colors(image_color_db, scope)

    assert image_color_db.stored[_KEY]["color"] is None
    assert image_color_db.stored[_KEY]["failed_at"] is not None


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
    compute_image_colors(image_color_db, scope)

    assert len(image_color_db.writes) == 2
    assert image_color_db.stored[f"{_HOST}-bad"]["color"] is None
    assert image_color_db.stored[f"{_HOST}-bad"]["failed_at"] is not None
    assert image_color_db.stored[f"{_HOST}-good"]["color"] == "#ee0000"


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
    compute_image_colors(image_color_db, scope)

    assert len(image_color_db.writes) == 2
    assert image_color_db.stored[f"{_HOST}-bad"]["color"] is None
    assert image_color_db.stored[f"{_HOST}-bad"]["failed_at"] is not None
    assert image_color_db.stored[f"{_HOST}-good"]["color"] == "#ee0000"


def test_the_real_fetch_raises_invalid_url_for_a_control_character(monkeypatch):
    # Why InvalidURL is in the tuple: it is not an httpx.HTTPError.
    _use_transport(monkeypatch, lambda request: httpx.Response(200))

    with pytest.raises(httpx.InvalidURL):
        _real_fetch_image("https://lh3.googleusercontent.com/\x01a")


def test_a_recent_failure_is_not_claimed_again(image_color_db):
    image_color_db.store(_KEY, None, failed=True)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    compute_image_colors(image_color_db, scope)

    assert scope.pending == {}
    assert image_color_db.claims == []


def test_a_failure_older_than_the_retry_window_is_claimed_again(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    old = datetime.now(UTC) - timedelta(seconds=settings.image_color_retry_after + 60)
    image_color_db.store(_KEY, None, attempted_at=old, failed=True)

    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    compute_image_colors(image_color_db, scope)

    assert image_color_db.claims == [[_KEY]]
    assert stub.calls == [f"{_HOST}=w192-h192-p"]
    assert image_color_db.stored[_KEY]["color"] == "#ee0000"
    assert image_color_db.stored[_KEY]["failed_at"] is None


def test_at_most_max_per_request_keys_are_claimed(
    image_color_db, monkeypatch, allow_image_downloads
):
    monkeypatch.setattr(settings, "image_color_max_per_request", 2)
    allow_image_downloads(lambda url: _red_png())
    urls = [f"{_HOST}{n}=w60-h60" for n in range(5)]

    scope = _load(image_color_db, *urls)
    compute_image_colors(image_color_db, scope)

    assert len(scope.pending) == 5
    assert image_color_db.claims == [[f"{_HOST}0", f"{_HOST}1"]]


def test_without_a_free_slot_nothing_is_claimed(image_color_db):
    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    held = []
    while image_color_service._slots.acquire(blocking=False):
        held.append(1)
    try:
        compute_image_colors(image_color_db, scope)
    finally:
        for _ in held:
            image_color_service._slots.release()

    assert image_color_db.claims == []


def test_a_failed_claim_is_logged_and_nothing_is_downloaded(
    image_color_db, allow_image_downloads, caplog
):
    stub = allow_image_downloads(lambda url: _red_png())
    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    image_color_db.claim_error = httpx.ConnectError("database down")

    compute_image_colors(image_color_db, scope)

    assert stub.calls == []
    assert "image_color claim failed" in caplog.text


def test_a_failed_write_is_logged_and_does_not_raise(
    image_color_db, allow_image_downloads, caplog
):
    allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    scope = _load(image_color_db, f"{_HOST}=w60-h60")
    image_color_db.write_error = httpx.ConnectError("database down")

    compute_image_colors(image_color_db, scope)

    assert "image_color write failed" in caplog.text


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
    compute_image_colors(db, scope)
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
    assert image_color_db.stored[_KEY]["color"] == "#ee0000"


def test_a_redirect_is_never_followed(image_color_db, monkeypatch):
    requests = _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://169.254.169.254/latest"}
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert len(requests) == 1
    assert image_color_db.stored[_KEY]["color"] is None
    assert image_color_db.stored[_KEY]["failed_at"] is not None


def test_a_non_image_content_type_fails(image_color_db, monkeypatch):
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=_red_png()
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_KEY]["failed_at"] is not None


def test_a_non_200_status_fails(image_color_db, monkeypatch):
    _use_transport(monkeypatch, lambda request: httpx.Response(404))

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_KEY]["failed_at"] is not None


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

    assert image_color_db.stored[_KEY]["failed_at"] is not None


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

    assert image_color_db.stored[_KEY]["failed_at"] is not None


def test_a_timeout_fails(image_color_db, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    _use_transport(monkeypatch, timeout)

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_KEY]["color"] is None
    assert image_color_db.stored[_KEY]["failed_at"] is not None


def test_an_image_over_the_pixel_cap_fails(image_color_db, monkeypatch):
    monkeypatch.setattr(settings, "image_color_max_pixels", 100)
    _use_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "image/png"}, content=_red_png()
        ),
    )

    _compute_with_real_fetch(image_color_db)

    assert image_color_db.stored[_KEY]["failed_at"] is not None
