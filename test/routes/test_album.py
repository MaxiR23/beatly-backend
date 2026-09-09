# test/routes/test_album.py
#
# Tests for the album endpoint.
#
# Tested:
# - GET /album/{album_id} returns the album mapped field by field from a
#   single call to the external provider (browseId -> id on referenced
#   albums, audioPlaylistId -> audio_playlist_id, trackCount ->
#   track_count, videoId -> track_id, trackNumber -> track_number,
#   largest thumbnail), including tracks, other_versions and
#   related_recommendations
# - data.id is the id requested in the path, not one from the provider's
#   response, which never carries it back
# - provider.get_album is called exactly once, with the requested
#   album_id: no per-track enrichment call
# - other_versions/related_recommendations absent from the provider's
#   response returns 200 with both as empty lists
# - An album with no strapline (artists: None), whose tracks inherit
#   that None, returns 200 with artists: [] on the album and on every
#   track
# - A track unavailable in the provider's response (isAvailable: false,
#   no duration_seconds, trackNumber: None, videoId: None) is still
#   present in the list with is_available: false, track_id: null,
#   duration_seconds: null, and track_number falling back to its
#   position, without failing the whole album
# - In a mixed album, available tracks keep the provider's trackNumber
#   and the numbering around an unavailable track stays contiguous
# - A track missing the isAvailable key entirely returns 502
#   upstream_error: that is a layout change, not a documented branch
# - An album missing trackCount returns 200 with track_count: null
# - A referenced album (other_versions/related_recommendations) missing
#   year and with audioPlaylistId: None returns 200 with both null
# - An album_id without the required prefix returns 422 invalid_request
#   without calling the provider
# - A well-formed but nonexistent album_id (KeyError from the provider)
#   returns 502 upstream_error, never 404
# - A network failure from the provider returns 502 upstream_error
# - A timeout from the provider returns 504 upstream_timeout
# - A server error from the provider returns 502 upstream_error
# - A ValueError raised while parsing the provider's response returns
#   502 upstream_error
# - An IndexError raised while parsing the provider's response returns
#   502 upstream_error
# - A malformed album (missing title, duration_seconds or tracks)
#   returns 502 upstream_error, not a 200 with null fields or a 500
# - No token returns 401 unauthorized without calling the provider
# - GET /album/ with no id returns 404 not_found, ok:false
#
# What is covered:
# - Happy path, id-from-path vs id-from-provider, single-call
#   contract, expected empty carousels, missing artist data, track
#   availability, invalid input, unauthenticated access, upstream
#   failure, upstream timeout, malformed upstream data, no-route 404
#
# Run with: pytest test/routes/test_album.py -v
#
# SEE: routes/album.py, services/album_service.py, core/search_provider.py

from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

from app import app
from core.auth import get_current_user_id
from core.search_provider import PROVIDER_ERRORS, get_search_provider

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_ALBUM_ID = "MPREb_0000000000001"

_TRACK_ONE = {
    "videoId": "track-1",
    "title": "Track One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "duration_seconds": 200,
    "isAvailable": True,
    "trackNumber": 1,
}

_TRACK_TWO = {
    "videoId": "track-2",
    "title": "Track Two",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "duration_seconds": 210,
    "isAvailable": True,
    "trackNumber": 2,
}

_OTHER_VERSION = {
    "browseId": "album-ref-1",
    "title": "Album One (Deluxe)",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "year": "2021",
    "audioPlaylistId": "OLAK5uy_deluxe",
    "thumbnails": [{"url": "https://example.com/deluxe.jpg"}],
}

_RELATED_RECOMMENDATION = {
    "browseId": "album-ref-2",
    "title": "Album Two",
    "artists": [{"id": "artist-2", "name": "Other Artist"}],
    "year": "2019",
    "audioPlaylistId": "OLAK5uy_related",
    "thumbnails": [{"url": "https://example.com/related.jpg"}],
}

_ALBUM_ROW = {
    "title": "Album One",
    "year": "2020",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "trackCount": 2,
    "duration_seconds": 410,
    "audioPlaylistId": "OLAK5uy_main",
    "thumbnails": [
        {"url": "https://example.com/album-small.jpg"},
        {"url": "https://example.com/album-large.jpg"},
    ],
    "tracks": [_TRACK_ONE, _TRACK_TWO],
    "other_versions": [_OTHER_VERSION],
    "related_recommendations": [_RELATED_RECOMMENDATION],
}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_search_provider, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _fake_provider(row=None, error=None):
    provider = MagicMock()
    if error is not None:
        provider.get_album.side_effect = error
    else:
        provider.get_album.return_value = row
    return provider


def _use_provider(provider):
    app.dependency_overrides[get_search_provider] = lambda: provider


# --- Happy path -------------------------------------------------------------


def test_get_album_returns_album_mapped_field_by_field():
    provider = _fake_provider(row=_ALBUM_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "id": _ALBUM_ID,
        "title": "Album One",
        "year": "2020",
        "artists": [{"id": "artist-1", "name": "Main Artist"}],
        "track_count": 2,
        "duration_seconds": 410,
        "audio_playlist_id": "OLAK5uy_main",
        "thumbnail_url": "https://example.com/album-large.jpg",
        "tracks": [
            {
                "track_id": "track-1",
                "title": "Track One",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "duration_seconds": 200,
                "is_available": True,
                "track_number": 1,
            },
            {
                "track_id": "track-2",
                "title": "Track Two",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "duration_seconds": 210,
                "is_available": True,
                "track_number": 2,
            },
        ],
        "other_versions": [
            {
                "id": "album-ref-1",
                "title": "Album One (Deluxe)",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "year": "2021",
                "audio_playlist_id": "OLAK5uy_deluxe",
                "thumbnail_url": "https://example.com/deluxe.jpg",
            }
        ],
        "related_recommendations": [
            {
                "id": "album-ref-2",
                "title": "Album Two",
                "artists": [{"id": "artist-2", "name": "Other Artist"}],
                "year": "2019",
                "audio_playlist_id": "OLAK5uy_related",
                "thumbnail_url": "https://example.com/related.jpg",
            }
        ],
    }


def test_get_album_id_is_the_requested_path_param():
    provider = _fake_provider(row=_ALBUM_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == _ALBUM_ID


def test_get_album_calls_provider_exactly_once_with_requested_id():
    provider = _fake_provider(row=_ALBUM_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    provider.get_album.assert_called_once_with(_ALBUM_ID)


def test_get_album_missing_carousels_returns_empty_lists():
    row = {
        k: v
        for k, v in _ALBUM_ROW.items()
        if k not in {"other_versions", "related_recommendations"}
    }
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["other_versions"] == []
    assert data["related_recommendations"] == []


def test_get_album_without_strapline_returns_empty_artists_on_album_and_tracks():
    track_without_artists = {**_TRACK_ONE, "artists": None}
    row = {**_ALBUM_ROW, "artists": None, "tracks": [track_without_artists]}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artists"] == []
    assert data["tracks"][0]["artists"] == []


def test_get_album_without_track_count_returns_null():
    row = {k: v for k, v in _ALBUM_ROW.items() if k != "trackCount"}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["track_count"] is None


def test_get_album_ref_without_year_or_playlist_id_returns_null():
    other_version = {k: v for k, v in _OTHER_VERSION.items() if k != "year"}
    other_version["audioPlaylistId"] = None
    row = {**_ALBUM_ROW, "other_versions": [other_version]}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    ref = response.json()["data"]["other_versions"][0]
    assert ref["year"] is None
    assert ref["audio_playlist_id"] is None


# --- Track availability (adenda) --------------------------------------------


def test_get_album_unavailable_track_is_kept_with_position_as_number():
    unavailable_track = {
        "videoId": None,
        "title": "Unavailable Track",
        "artists": [{"id": "artist-1", "name": "Main Artist"}],
        "isAvailable": False,
        "trackNumber": None,
    }
    row = {**_ALBUM_ROW, "tracks": [_TRACK_ONE, unavailable_track]}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    track = response.json()["data"]["tracks"][1]
    assert track["is_available"] is False
    assert track["track_id"] is None
    assert track["duration_seconds"] is None
    assert track["track_number"] == 2


def test_get_album_mixed_availability_keeps_numbering_contiguous():
    unavailable_track = {
        "videoId": None,
        "title": "Unavailable Track",
        "artists": [{"id": "artist-1", "name": "Main Artist"}],
        "isAvailable": False,
        "trackNumber": None,
    }
    third_track = {**_TRACK_TWO, "videoId": "track-3", "trackNumber": 3}
    row = {**_ALBUM_ROW, "tracks": [_TRACK_ONE, unavailable_track, third_track]}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    tracks = response.json()["data"]["tracks"]
    assert [t["track_number"] for t in tracks] == [1, 2, 3]
    assert tracks[0]["is_available"] is True
    assert tracks[1]["is_available"] is False
    assert tracks[2]["is_available"] is True


def test_get_album_track_missing_is_available_key_returns_upstream_error():
    track_without_flag = {k: v for k, v in _TRACK_ONE.items() if k != "isAvailable"}
    row = {**_ALBUM_ROW, "tracks": [track_without_flag]}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# --- Invalid input / auth ---------------------------------------------------


def test_get_album_id_without_prefix_returns_invalid_request():
    provider = _fake_provider(row=_ALBUM_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get("/album/some-song-id")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    provider.get_album.assert_not_called()


def test_get_album_unauthenticated_returns_unauthorized():
    provider = _fake_provider(row=_ALBUM_ROW)
    _use_provider(provider)

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    provider.get_album.assert_not_called()


def test_get_album_with_no_id_returns_not_found():
    provider = _fake_provider(row=_ALBUM_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get("/album/")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "not_found"}
    provider.get_album.assert_not_called()


# --- Upstream failures -------------------------------------------------------


def test_get_album_nonexistent_id_returns_upstream_error_not_not_found():
    provider = _fake_provider(error=KeyError("microformat"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_album_provider_network_failure_returns_upstream_error():
    provider = _fake_provider(error=requests.exceptions.ConnectionError())
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_album_provider_timeout_returns_upstream_timeout():
    provider = _fake_provider(error=requests.exceptions.Timeout())
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_album_provider_server_error_returns_upstream_error():
    provider = _fake_provider(error=PROVIDER_ERRORS[0]("server error"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_album_provider_value_error_returns_upstream_error():
    provider = _fake_provider(error=ValueError("layout changed"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_album_provider_index_error_returns_upstream_error():
    provider = _fake_provider(error=IndexError("layout changed"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize(
    "malformed_row",
    [
        {k: v for k, v in _ALBUM_ROW.items() if k != "title"},
        {k: v for k, v in _ALBUM_ROW.items() if k != "duration_seconds"},
        {k: v for k, v in _ALBUM_ROW.items() if k != "tracks"},
    ],
)
def test_get_album_malformed_row_returns_upstream_error(malformed_row):
    provider = _fake_provider(row=malformed_row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
