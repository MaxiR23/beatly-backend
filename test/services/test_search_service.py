# test/services/test_search_service.py
#
# Tests for find_artist, the public entry of the search service used by the
# onboarding seed script.
#
# Tested:
# - Returns the first row of the artist search, with the photo at 544 x 544
#   with smart crop
# - Asks the provider for one artist row, with no cache
# - Returns None for a search without results and for a row that does not
#   fit the model
# - A provider failure is UpstreamError and a timeout UpstreamTimeout
#
# What is covered:
# - services/search_service.py find_artist; /search is in
#   test/routes/test_search.py
#
# Run with: pytest test/services/test_search_service.py -v
#
# SEE: services/search_service.py, scripts/seed_onboarding_artists.py

from unittest.mock import MagicMock

import pytest
import requests

from core.exceptions import UpstreamError, UpstreamTimeout
from core.search_provider import PROVIDER_ERRORS
from services.search_service import find_artist

_HOST = "https://lh3.googleusercontent.com/abc"


def _provider(rows):
    provider = MagicMock()
    provider.search.return_value = rows
    return provider


def test_find_artist_returns_the_first_artist_with_the_photo_at_544():
    provider = _provider(
        [
            {
                "browseId": "UCone",
                "artist": "One",
                "thumbnails": [{"url": f"{_HOST}=w226-h226-l90-rj"}],
            },
            {"browseId": "UCtwo", "artist": "Two", "thumbnails": []},
        ]
    )

    artist = find_artist(provider, "one")

    assert artist is not None
    assert (artist.id, artist.name) == ("UCone", "One")
    assert artist.thumbnail_url == f"{_HOST}=w544-h544-p-l90-rj"


def test_find_artist_asks_for_one_artist_row():
    provider = _provider([])

    find_artist(provider, "one")

    provider.search.assert_called_once()
    assert provider.search.call_args.args[0] == "one"
    assert provider.search.call_args.kwargs["filter"] == "artists"
    assert provider.search.call_args.kwargs["limit"] == 1


def test_find_artist_without_results_is_none():
    assert find_artist(_provider([]), "nobody") is None


def test_find_artist_with_an_incomplete_row_is_none():
    assert find_artist(_provider([{"artist": "No id"}]), "x") is None


def test_find_artist_provider_failure_is_upstream_error():
    provider = MagicMock()
    provider.search.side_effect = PROVIDER_ERRORS[0]("server error")

    with pytest.raises(UpstreamError):
        find_artist(provider, "x")


def test_find_artist_provider_timeout_is_upstream_timeout():
    provider = MagicMock()
    provider.search.side_effect = requests.exceptions.ReadTimeout("slow")

    with pytest.raises(UpstreamTimeout):
        find_artist(provider, "x")
