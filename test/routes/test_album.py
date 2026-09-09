# test/routes/test_album.py
#
# Tests for the album endpoint.
#
# Tested:
# - GET /album/{album_id} returns the album mapped field by field, with
#   the album fields (year, artists, trackCount, audioPlaylistId,
#   thumbnails, other_versions, related_recommendations) coming from a
#   first call to the provider (get_album) and data.tracks coming from
#   a second call, to the album's audio playlist (get_playlist)
# - data.tracks[].track_id is the audio playlist item's videoId, never
#   the videoId carried by the album payload's own "tracks" array: a
#   dedicated test proves the two are distinguishable and that the
#   album payload's ids never leak into the response
# - data.id is the id requested in the path, not one from the provider's
#   response, which never carries it back
# - provider.get_album is called exactly once with the requested
#   album_id, provider.get_playlist is called exactly once with the
#   album's audioPlaylistId and limit=None, and provider.get_song is
#   never called: no per-track enrichment call
# - track_number always falls back to the track's position, because the
#   audio playlist never carries trackNumber, and stays contiguous
#   around an unavailable track
# - track_count keeps coming from the album payload's own trackCount
#   even when the audio playlist's trackCount disagrees
# - An album payload with no "tracks" key at all still returns 200 with
#   the audio playlist's tracks: that key is no longer read
# - other_versions/related_recommendations absent from the provider's
#   response returns 200 with both as empty lists
# - An album with no strapline (artists: None) returns 200 with
#   artists: [] on the album
# - A playlist item with no artists returns artists: [] on that track
#   even when the album itself has artists: the album-to-track artist
#   inheritance the provider's own album parser does is gone by design
#   and is not reimplemented
# - A playlist item that is unavailable (isAvailable: false, videoId:
#   None, no duration_seconds) is still present in the list with
#   is_available: false, track_id: null, duration_seconds: null, and
#   track_number falling back to its position, without failing the
#   whole album
# - In a mixed playlist, the numbering around an unavailable track
#   stays contiguous
# - A playlist item missing the isAvailable key, or missing title,
#   returns 502 upstream_error: a layout change, not a documented
#   branch
# - An album missing trackCount returns 200 with track_count: null
# - A referenced album (other_versions/related_recommendations) missing
#   year and with audioPlaylistId: None returns 200 with both null
# - An album with audioPlaylistId: None returns 200 with data.tracks: []
#   and the rest of the album populated, and never calls get_playlist
# - That same case emits exactly one WARNING log record carrying the
#   album_id and nothing from the provider
# - An album with audioPlaylistId as an empty string is treated the
#   same as None: 200, data.tracks: [], no call to get_playlist
# - A network failure, a timeout, a server error, a ValueError, an
#   IndexError (the real shape of an empty audio playlist) or a
#   KeyError raised by the audio playlist call each return
#   502/504, never 404, with get_album responding fine
# - An audio playlist response missing the "tracks" key returns 502,
#   never a 200 with an empty list
# - An album_id without the required prefix returns 422 invalid_request
#   without calling either provider method
# - A well-formed but nonexistent album_id (KeyError from the provider)
#   returns 502 upstream_error, never 404
# - A network failure, timeout, server error, ValueError, IndexError or
#   KeyError from the first (get_album) call returns 502/504, never 404
# - A malformed album (missing title or duration_seconds) returns 502
#   upstream_error, not a 200 with null fields or a 500
# - No token returns 401 unauthorized without calling either provider
#   method
# - GET /album/ with no id returns 404 not_found, ok:false
#
# What is covered:
# - Happy path, id-from-path vs id-from-provider, the two-call
#   contract, expected empty carousels, missing artist data, the
#   removed album-to-track artist inheritance, track availability,
#   audio_playlist_id null, invalid input, unauthenticated access,
#   upstream failure and timeout on both calls, malformed upstream
#   data, no-route 404
#
# Run with: pytest test/routes/test_album.py -v
#
# SEE: routes/album.py, services/album_service.py, core/search_provider.py

import logging
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

# Deliberately different videoIds from the audio playlist's tracks
# below: the album payload's own "tracks" array is never read after
# this fix, and these ids exist only to prove it. A test that
# accidentally reads row["tracks"] again would leak "video-1"/"video-2"
# into the response instead of "track-1"/"track-2".
_TRACK_ONE = {
    "videoId": "video-1",
    "title": "Track One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "duration_seconds": 200,
    "isAvailable": True,
    "trackNumber": 1,
}

_TRACK_TWO = {
    "videoId": "video-2",
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

# The real shape of an item of ytmusicapi's parse_audio_playlist: it
# never carries trackNumber (only the album-payload branch does).
_PLAYLIST_TRACK_ONE = {
    "videoId": "track-1",
    "title": "Track One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "duration_seconds": 200,
    "isAvailable": True,
    "videoType": "MUSIC_VIDEO_TYPE_ATV",
    "thumbnails": [{"url": "https://example.com/track-1.jpg"}],
}

_PLAYLIST_TRACK_TWO = {
    "videoId": "track-2",
    "title": "Track Two",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "duration_seconds": 210,
    "isAvailable": True,
    "videoType": "MUSIC_VIDEO_TYPE_ATV",
    "thumbnails": [{"url": "https://example.com/track-2.jpg"}],
}

_PLAYLIST_ROW = {
    "id": "OLAK5uy_main",
    "tracks": [_PLAYLIST_TRACK_ONE, _PLAYLIST_TRACK_TWO],
    "trackCount": 2,
}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_search_provider, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _fake_provider(row=None, error=None, playlist=None, playlist_error=None):
    provider = MagicMock()
    if error is not None:
        provider.get_album.side_effect = error
    else:
        provider.get_album.return_value = row
    if playlist_error is not None:
        provider.get_playlist.side_effect = playlist_error
    else:
        provider.get_playlist.return_value = playlist
    return provider


def _use_provider(provider):
    app.dependency_overrides[get_search_provider] = lambda: provider


# --- Happy path -------------------------------------------------------------


def test_get_album_returns_album_mapped_field_by_field():
    provider = _fake_provider(row=_ALBUM_ROW, playlist=_PLAYLIST_ROW)
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


def test_get_album_track_ids_never_come_from_the_album_payload():
    provider = _fake_provider(row=_ALBUM_ROW, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    track_ids = [t["track_id"] for t in response.json()["data"]["tracks"]]
    assert "video-1" not in track_ids
    assert "video-2" not in track_ids
    assert track_ids == ["track-1", "track-2"]


def test_get_album_id_is_the_requested_path_param():
    provider = _fake_provider(row=_ALBUM_ROW, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == _ALBUM_ID


def test_get_album_calls_both_provider_methods_exactly_once():
    provider = _fake_provider(row=_ALBUM_ROW, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    provider.get_album.assert_called_once_with(_ALBUM_ID)
    provider.get_playlist.assert_called_once_with("OLAK5uy_main", limit=None)
    assert provider.get_song.call_count == 0


def test_get_album_track_number_falls_back_to_position_without_trackNumber():
    provider = _fake_provider(row=_ALBUM_ROW, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    track_numbers = [t["track_number"] for t in response.json()["data"]["tracks"]]
    assert track_numbers == [1, 2]


def test_get_album_track_count_comes_from_album_header_not_playlist():
    playlist = {**_PLAYLIST_ROW, "trackCount": 99}
    provider = _fake_provider(row=_ALBUM_ROW, playlist=playlist)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["track_count"] == 2


def test_get_album_row_without_tracks_key_still_returns_playlist_tracks():
    row = {k: v for k, v in _ALBUM_ROW.items() if k != "tracks"}
    provider = _fake_provider(row=row, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    track_ids = [t["track_id"] for t in response.json()["data"]["tracks"]]
    assert track_ids == ["track-1", "track-2"]


def test_get_album_missing_carousels_returns_empty_lists():
    row = {
        k: v
        for k, v in _ALBUM_ROW.items()
        if k not in {"other_versions", "related_recommendations"}
    }
    provider = _fake_provider(row=row, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["other_versions"] == []
    assert data["related_recommendations"] == []


def test_get_album_without_strapline_returns_empty_artists_on_album():
    row = {**_ALBUM_ROW, "artists": None}
    provider = _fake_provider(row=row, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["artists"] == []


def test_get_album_track_without_artists_does_not_inherit_album_artists():
    track_without_artists = {**_PLAYLIST_TRACK_ONE, "artists": None}
    playlist = {**_PLAYLIST_ROW, "tracks": [track_without_artists, _PLAYLIST_TRACK_TWO]}
    # The album itself does have artists, so a track inheriting them
    # would show "Main Artist" here. It must not: the provider's
    # album-to-track artist inheritance only exists in the get_album
    # parsing branch, which the audio playlist never goes through, and
    # this fix does not reimplement it.
    provider = _fake_provider(row=_ALBUM_ROW, playlist=playlist)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["artists"] == [
        {"id": "artist-1", "name": "Main Artist"}
    ]
    assert response.json()["data"]["tracks"][0]["artists"] == []


def test_get_album_without_track_count_returns_null():
    row = {k: v for k, v in _ALBUM_ROW.items() if k != "trackCount"}
    provider = _fake_provider(row=row, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["track_count"] is None


def test_get_album_ref_without_year_or_playlist_id_returns_null():
    other_version = {k: v for k, v in _OTHER_VERSION.items() if k != "year"}
    other_version["audioPlaylistId"] = None
    row = {**_ALBUM_ROW, "other_versions": [other_version]}
    provider = _fake_provider(row=row, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    ref = response.json()["data"]["other_versions"][0]
    assert ref["year"] is None
    assert ref["audio_playlist_id"] is None


# --- Track availability -------------------------------------------------------


def test_get_album_unavailable_track_is_kept_with_position_as_number():
    unavailable_track = {
        "videoId": None,
        "title": "Unavailable Track",
        "artists": [{"id": "artist-1", "name": "Main Artist"}],
        "isAvailable": False,
    }
    playlist = {**_PLAYLIST_ROW, "tracks": [_PLAYLIST_TRACK_ONE, unavailable_track]}
    provider = _fake_provider(row=_ALBUM_ROW, playlist=playlist)
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
    }
    third_track = {**_PLAYLIST_TRACK_TWO, "videoId": "track-3"}
    playlist = {
        **_PLAYLIST_ROW,
        "tracks": [_PLAYLIST_TRACK_ONE, unavailable_track, third_track],
    }
    provider = _fake_provider(row=_ALBUM_ROW, playlist=playlist)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    tracks = response.json()["data"]["tracks"]
    assert [t["track_number"] for t in tracks] == [1, 2, 3]
    assert tracks[0]["is_available"] is True
    assert tracks[1]["is_available"] is False
    assert tracks[2]["is_available"] is True


def test_get_album_playlist_track_missing_is_available_key_returns_upstream_error():
    track_without_flag = {
        k: v for k, v in _PLAYLIST_TRACK_ONE.items() if k != "isAvailable"
    }
    playlist = {**_PLAYLIST_ROW, "tracks": [track_without_flag]}
    provider = _fake_provider(row=_ALBUM_ROW, playlist=playlist)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_album_playlist_track_missing_title_returns_upstream_error():
    track_without_title = {k: v for k, v in _PLAYLIST_TRACK_ONE.items() if k != "title"}
    playlist = {**_PLAYLIST_ROW, "tracks": [track_without_title]}
    provider = _fake_provider(row=_ALBUM_ROW, playlist=playlist)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# --- audio_playlist_id null (expected empty) ---------------------------------


def test_get_album_without_audio_playlist_id_returns_empty_tracks():
    row = {**_ALBUM_ROW, "audioPlaylistId": None}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tracks"] == []
    assert data["title"] == "Album One"
    assert data["year"] == "2020"
    assert data["artists"] == [{"id": "artist-1", "name": "Main Artist"}]
    assert data["track_count"] == 2
    assert data["thumbnail_url"] == "https://example.com/album-large.jpg"
    assert len(data["other_versions"]) == 1
    assert len(data["related_recommendations"]) == 1
    provider.get_playlist.assert_not_called()


def test_get_album_without_audio_playlist_id_logs_a_warning_with_no_provider_detail(
    caplog,
):
    row = {**_ALBUM_ROW, "audioPlaylistId": None}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    with caplog.at_level(logging.WARNING):
        response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert _ALBUM_ID in message
    assert "Album One" not in message


def test_get_album_with_empty_string_audio_playlist_id_returns_empty_tracks():
    row = {**_ALBUM_ROW, "audioPlaylistId": ""}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["tracks"] == []
    provider.get_playlist.assert_not_called()


# --- Invalid input / auth ---------------------------------------------------


def test_get_album_id_without_prefix_returns_invalid_request():
    provider = _fake_provider(row=_ALBUM_ROW, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get("/album/some-song-id")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    provider.get_album.assert_not_called()
    provider.get_playlist.assert_not_called()


def test_get_album_unauthenticated_returns_unauthorized():
    provider = _fake_provider(row=_ALBUM_ROW, playlist=_PLAYLIST_ROW)
    _use_provider(provider)

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    provider.get_album.assert_not_called()
    provider.get_playlist.assert_not_called()


def test_get_album_with_no_id_returns_not_found():
    provider = _fake_provider(row=_ALBUM_ROW, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get("/album/")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "not_found"}
    provider.get_album.assert_not_called()
    provider.get_playlist.assert_not_called()


# --- Upstream failures on the first call (get_album) -------------------------


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
    ],
)
def test_get_album_malformed_row_returns_upstream_error(malformed_row):
    provider = _fake_provider(row=malformed_row, playlist=_PLAYLIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# --- Upstream failures on the second call (get_playlist) ---------------------


@pytest.mark.parametrize(
    "playlist_error,expected_status,expected_reason",
    [
        (requests.exceptions.ConnectionError(), 502, "upstream_error"),
        (requests.exceptions.Timeout(), 504, "upstream_timeout"),
        (PROVIDER_ERRORS[0]("server error"), 502, "upstream_error"),
        (ValueError("layout changed"), 502, "upstream_error"),
        # IndexError is the real shape of an empty or unparseable audio
        # playlist: parse_audio_playlist indexes playlist["tracks"][0]
        # to build the playlist's own title, and an empty tracks list
        # makes that raise IndexError inside the library. Without the
        # wrapper's (ValueError, IndexError) translation this would be
        # a 500, not a 502.
        (IndexError("layout changed"), 502, "upstream_error"),
        (KeyError("contents"), 502, "upstream_error"),
    ],
)
def test_get_album_playlist_failure_returns_upstream_error(
    playlist_error, expected_status, expected_reason
):
    provider = _fake_provider(row=_ALBUM_ROW, playlist_error=playlist_error)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == expected_status
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": expected_reason}
    # Empty for the two exceptions raised with no message
    # (ConnectionError, Timeout): guard against the trivial "" in
    # anything before asserting the message is not leaked.
    if str(playlist_error):
        assert str(playlist_error) not in response.text


def test_get_album_playlist_without_tracks_key_returns_upstream_error():
    playlist = {k: v for k, v in _PLAYLIST_ROW.items() if k != "tracks"}
    provider = _fake_provider(row=_ALBUM_ROW, playlist=playlist)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
