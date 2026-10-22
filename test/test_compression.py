# test/test_compression.py
#
# Tests for gzip compression of responses.
#
# Tested:
# - A large response with Accept-Encoding gzip is compressed and
#   decompresses to the identity bytes
# - A large response with identity is not compressed
# - A large response varies on Accept-Encoding with or without gzip
# - /health with gzip is not compressed and has no Vary
# - A compressed response keeps its route Cache-Control, once
# - Small errors (404, 422) with gzip are not compressed
# - A large error with gzip decompresses to the error envelope
# - The unhandled 500 is never compressed
# - GZipMiddleware sits inside default_cache_control with the settings
#   values
#
# What is covered:
# - The GZipMiddleware wiring in app.py and the gzip_* settings
#
# Run with: pytest test/test_compression.py -v
#
# SEE: app.py, core/config.py

import gzip
import json

import pytest
from fastapi import Depends
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

from app import app, default_cache_control
from core.cache_control import private_no_cache
from core.config import settings
from core.exceptions import NotFound
from models.responses import ok_response

client = TestClient(app, raise_server_exceptions=False)

_SECRET_MESSAGE = "leaked db password abc123"
_LONG_REASON = "r" * (settings.gzip_minimum_size + 1)


def _large_body():
    return ok_response(list(range(settings.gzip_minimum_size)))


@app.get("/_test/compression/large")
def _large():
    return _large_body()


@app.get("/_test/compression/large-private", dependencies=[Depends(private_no_cache)])
def _large_private():
    return _large_body()


@app.get("/_test/compression/large-error")
def _large_error():
    raise NotFound(_LONG_REASON)


@app.get("/_test/compression/small-not-found")
def _small_not_found():
    raise NotFound()


@app.get("/_test/compression/requires-int")
def _requires_int(count: int):
    return {"count": count}


@app.get("/_test/compression/raises-unhandled")
def _raises_unhandled():
    raise ValueError(_SECRET_MESSAGE)


def _raw(path, encoding, params=None):
    with client.stream(
        "GET", path, headers={"Accept-Encoding": encoding}, params=params
    ) as response:
        return response, b"".join(response.iter_raw())


def test_large_response_with_gzip_decompresses_to_the_identity_bytes():
    _, identity = _raw("/_test/compression/large", "identity")
    assert len(identity) >= settings.gzip_minimum_size

    response, raw = _raw("/_test/compression/large", "gzip")

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-length"] == str(len(raw))
    assert gzip.decompress(raw) == identity
    assert json.loads(gzip.decompress(raw))["ok"] is True


def test_large_response_with_identity_is_not_compressed():
    response, raw = _raw("/_test/compression/large", "identity")

    assert len(raw) >= settings.gzip_minimum_size
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(raw))
    assert raw.startswith(b'{"ok":true,"data":')


@pytest.mark.parametrize("encoding", ["gzip", "identity"])
def test_large_response_varies_on_accept_encoding_with_or_without_gzip(encoding):
    response, _ = _raw("/_test/compression/large", encoding)

    assert response.headers["vary"] == "Accept-Encoding"


def test_health_with_gzip_is_not_compressed():
    # This test breaks if the gzip is put outside default_cache_control.
    response, raw = _raw("/health", "gzip")

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert "vary" not in response.headers
    assert response.headers["content-length"] == "38"
    assert raw == b'{"ok":true,"data":{"version":"0.1.0"}}'
    assert response.headers.get_list("cache-control") == ["no-store"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/_test/compression/large", "no-store"),
        ("/_test/compression/large-private", "private, no-cache"),
    ],
)
def test_compressed_response_keeps_its_route_cache_control_once(path, expected):
    response, raw = _raw(path, "gzip")

    assert len(gzip.decompress(raw)) >= settings.gzip_minimum_size
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers.get_list("cache-control") == [expected]


@pytest.mark.parametrize(
    ("path", "params", "status", "reason"),
    [
        ("/this-route-does-not-exist", None, 404, "not_found"),
        ("/_test/compression/small-not-found", None, 404, "not_found"),
        ("/_test/compression/requires-int", {"count": "x"}, 422, "invalid_request"),
    ],
)
def test_small_error_with_gzip_is_not_compressed(path, params, status, reason):
    response, raw = _raw(path, "gzip", params)

    assert response.status_code == status
    assert json.loads(raw) == {"ok": False, "reason": reason}
    assert "content-encoding" not in response.headers
    assert "vary" not in response.headers
    assert response.headers.get_list("cache-control") == ["no-store"]


def test_large_error_with_gzip_decompresses_to_the_error_envelope():
    response, raw = _raw("/_test/compression/large-error", "gzip")

    assert response.status_code == 404
    assert response.headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(raw)) == {"ok": False, "reason": _LONG_REASON}
    assert response.headers.get_list("cache-control") == ["no-store"]


def test_unhandled_error_with_gzip_is_unchanged():
    response, raw = _raw("/_test/compression/raises-unhandled", "gzip")

    assert response.status_code == 500
    assert "content-encoding" not in response.headers
    assert raw == b'{"ok":false,"reason":"internal_error"}'
    assert response.headers.get_list("cache-control") == ["no-store"]
    assert _SECRET_MESSAGE.encode() not in raw


def test_gzip_runs_inside_default_cache_control_with_the_settings_values():
    middleware = app.user_middleware
    gzip_indexes = [i for i, m in enumerate(middleware) if m.cls is GZipMiddleware]
    cache_indexes = [
        i
        for i, m in enumerate(middleware)
        if m.kwargs.get("dispatch") is default_cache_control
    ]

    assert len(gzip_indexes) == 1
    assert len(cache_indexes) == 1
    assert middleware[gzip_indexes[0]].kwargs == {
        "minimum_size": settings.gzip_minimum_size,
        "compresslevel": settings.gzip_compresslevel,
    }
    assert gzip_indexes[0] > cache_indexes[0]
