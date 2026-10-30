# test/core/test_search_provider.py
#
# Tests for how the external provider client is built.
#
# Tested:
# - _provider() builds the client with the bounded ProviderSession
# - provider_raise_if_browse_missing raises ProviderResourceMissing only on
#   the measured missing shape (no contents and noindex), from the cause
# - It returns when the page has contents or urlCanonical (a layout change)
# - A response without microformat is a KeyError, never a missing id
# - It strips the MPLA prefix and turns ValueError into a parse error
# - The library still exposes _send_request(endpoint, body)
#
# What is covered:
# - The wiring of core/search_provider.py to core/provider_session.py
#
# Run with: pytest test/core/test_search_provider.py -v
#
# SEE: core/search_provider.py, core/provider_session.py

import inspect
from unittest.mock import MagicMock

import pytest

from core import search_provider
from core.provider_session import ProviderSession
from core.search_provider import (
    ProviderParseError,
    ProviderResourceMissing,
    SearchProvider,
    provider_raise_if_browse_missing,
)

_MISSING_BROWSE = {
    "responseContext": {},
    "trackingParams": "t",
    "microformat": {"microformatDataRenderer": {"noindex": True}},
}
_EXISTING_BROWSE = {
    "responseContext": {},
    "trackingParams": "t",
    "contents": {},
    "microformat": {
        "microformatDataRenderer": {
            "urlCanonical": "https://example.com/x",
            "title": "t",
        }
    },
}


def _provider_answering(raw):
    provider = MagicMock()
    provider._send_request.return_value = raw
    return provider


def test_provider_is_built_with_the_bounded_session(monkeypatch):
    built = {}

    def _recorder(**kwargs):
        built.update(kwargs)
        return object()

    monkeypatch.setattr(search_provider, "SearchProvider", _recorder)
    search_provider._provider.cache_clear()
    try:
        search_provider._provider()
    finally:
        search_provider._provider.cache_clear()

    assert isinstance(built["requests_session"], ProviderSession)


def test_browse_probe_raises_resource_missing_on_the_measured_missing_shape():
    provider = _provider_answering(_MISSING_BROWSE)
    cause = KeyError("contents")

    with pytest.raises(ProviderResourceMissing, match="UCmissing") as excinfo:
        provider_raise_if_browse_missing(provider, "UCmissing", cause)

    assert excinfo.value.__cause__ is cause
    provider._send_request.assert_called_once_with("browse", {"browseId": "UCmissing"})


def test_browse_probe_returns_when_the_page_has_contents():
    provider = _provider_answering(_EXISTING_BROWSE)

    assert provider_raise_if_browse_missing(provider, "UCx", KeyError("c")) is None


def test_browse_probe_needs_both_conditions():
    no_contents_but_canonical = {
        "microformat": {
            "microformatDataRenderer": {"urlCanonical": "https://example.com/x"}
        }
    }
    contents_but_noindex = {
        "contents": {},
        "microformat": {"microformatDataRenderer": {"noindex": True}},
    }

    for raw in (no_contents_but_canonical, contents_but_noindex):
        provider = _provider_answering(raw)
        assert provider_raise_if_browse_missing(provider, "UCx", KeyError("c")) is None


def test_browse_probe_without_microformat_raises_keyerror():
    provider = _provider_answering({"responseContext": {}})

    with pytest.raises(KeyError):
        provider_raise_if_browse_missing(provider, "UCx", KeyError("c"))


def test_browse_probe_strips_the_mpla_prefix():
    provider = _provider_answering(_EXISTING_BROWSE)

    provider_raise_if_browse_missing(provider, "MPLAUCabc", KeyError("c"))

    provider._send_request.assert_called_once_with("browse", {"browseId": "UCabc"})


def test_browse_probe_keeps_an_album_id_as_it_is():
    provider = _provider_answering(_EXISTING_BROWSE)

    provider_raise_if_browse_missing(provider, "MPREb_abc", KeyError("c"))

    provider._send_request.assert_called_once_with("browse", {"browseId": "MPREb_abc"})


def test_browse_probe_value_error_is_a_parse_error():
    provider = MagicMock()
    provider._send_request.side_effect = ValueError("bad json")

    with pytest.raises(ProviderParseError):
        provider_raise_if_browse_missing(provider, "UCx", KeyError("c"))


def test_the_library_still_exposes_send_request_with_endpoint_and_body():
    names = list(inspect.signature(SearchProvider._send_request).parameters)

    assert names[1:3] == ["endpoint", "body"]
