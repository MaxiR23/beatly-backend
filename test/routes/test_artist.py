# test/routes/test_artist.py
#
# Tests for the artist endpoint.
#
# Tested:
# - GET /artist/{artist_id} returns the artist mapped field by field from
#   a single call to the external provider (browseId -> id, title -> name
#   on related artists, videoId -> track_id, largest thumbnail), including
#   songs, albums, singles and related artists
# - data.id is the id requested in the path, not the provider's channelId,
#   which identifies a different (video) channel
# - provider.get_artist is called exactly once, with the requested
#   artist_id
# - songs missing from the provider's response returns 200 with
#   data.songs: []
# - songs present but without a "results" key returns 200 with
#   data.songs: [] ("API sometimes does not return songs")
# - albums/singles/related missing from the provider's response each
#   return 200 with that list as []
# - albums/singles/related present but without a "results" key returns
#   502 upstream_error: unlike songs, that is a layout change, not a
#   documented branch, and must never surface as a silently empty list
# - Any of the four sections arriving as something other than a dict
#   returns 502 upstream_error, never 500
# - data.singles mixes singles and EPs in a single list, in the
#   provider's order
# - A single/EP missing year and type returns 200 with both null
# - A song whose album has a name and an id returns both album and
#   album_id populated
# - A song whose album is entirely null returns album and album_id both
#   null
# - A song whose album has a name but a null id returns album and
#   album_id BOTH null, losing the name on purpose (album/album_id must
#   always travel together)
# - A degraded song (videoId: None, artists: None, no duration_seconds
#   key, thumbnails: None) still appears in the list with track_id,
#   duration_seconds and thumbnail_url null and artists: []
# - An artist with thumbnails: None returns 200 with thumbnail_url: null
# - An artist_id not matching the required pattern returns 422
#   invalid_request without calling the provider
# - An artist_id with the MPLA prefix is accepted and reaches the
#   provider
# - A well-formed but nonexistent artist_id (KeyError from the provider)
#   returns 502 upstream_error, never 404
# - A network failure from the provider returns 502 upstream_error
# - A timeout from the provider returns 504 upstream_timeout
# - A server error from the provider returns 502 upstream_error
# - A ValueError raised while parsing the provider's response returns
#   502 upstream_error
# - An IndexError raised while parsing the provider's response returns
#   502 upstream_error
# - A malformed artist (missing name, missing thumbnails, or a song
#   missing title) returns 502 upstream_error, not a 200 with null
#   fields or a 500
# - No token returns 401 unauthorized without calling the provider
# - GET /artist/ with no id returns 404 not_found, ok:false
# - A cache hit returns the cached body without calling the provider
# - A cache miss writes the response with key beatly:v1:artist:{id} and
#   ex=43200
# - A 502 is never written to cache: a second request with the same id
#   still calls the provider
# - A Redis failure on read or on write still returns 200 with the
#   provider's data
# - A cached value that fails to deserialize falls back to the provider,
#   never 502
#
# What is covered:
# - Happy path, id-from-path vs id-from-provider, single-call contract,
#   expected empty sections (by absence), invalid input, unauthenticated
#   access, upstream failure, upstream timeout, malformed upstream data,
#   no-route 404, cache hit/miss/failure and corrupted value
#
# Run with: pytest test/routes/test_artist.py -v
#
# SEE: routes/artist.py, services/artist_service.py, core/search_provider.py,
# core/cache.py

import json
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app import app
from core.auth import get_current_user_id
from core.cache import get_redis
from core.search_provider import PROVIDER_ERRORS, get_search_provider

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_ARTIST_ID = "UC1111111111111111111111"

_SONG_ONE = {
    "videoId": "song-1",
    "title": "Song One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "album": {"name": "Album One", "id": "MPREb_album1"},
    "duration_seconds": 200,
    "thumbnails": [
        {"url": "https://example.com/song-1-small.jpg"},
        {"url": "https://example.com/song-1-large.jpg"},
    ],
}

_SONG_TWO = {
    "videoId": "song-2",
    "title": "Song Two",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "album": {"name": "Album One", "id": "MPREb_album1"},
    "duration_seconds": 210,
    "thumbnails": [{"url": "https://example.com/song-2.jpg"}],
}

_ALBUM_ONE = {
    "browseId": "MPREb_album1",
    "title": "Album One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "year": "2020",
    "audioPlaylistId": "OLAK5uy_album1",
    "thumbnails": [
        {"url": "https://example.com/album-small.jpg"},
        {"url": "https://example.com/album-large.jpg"},
    ],
}

_SINGLE_ONE = {
    "browseId": "MPREb_single1",
    "title": "Single One",
    "year": "2019",
    "type": "Single",
    "thumbnails": [{"url": "https://example.com/single.jpg"}],
}

_EP_ONE = {
    "browseId": "MPREb_ep1",
    "title": "EP One",
    "year": "2018",
    "type": "EP",
    "thumbnails": [{"url": "https://example.com/ep.jpg"}],
}

_RELATED_ONE = {
    "browseId": "UC-related-1",
    "title": "Related Artist",
    "subscribers": "1.2M",
    "thumbnails": [{"url": "https://example.com/related.jpg"}],
}

_ARTIST_ROW = {
    # channelId is a different (video) channel than the one requested;
    # the service must never read it back as data.id.
    "channelId": "UC-video-channel-does-not-match-request",
    "name": "Main Artist",
    "thumbnails": [
        {"url": "https://example.com/artist-small.jpg"},
        {"url": "https://example.com/artist-large.jpg"},
    ],
    "songs": {"browseId": "some-playlist-id", "results": [_SONG_ONE, _SONG_TWO]},
    "albums": {"browseId": None, "results": [_ALBUM_ONE]},
    "singles": {"browseId": None, "results": [_SINGLE_ONE, _EP_ONE]},
    "related": {"browseId": None, "results": [_RELATED_ONE]},
}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_search_provider, None)
    app.dependency_overrides.pop(get_current_user_id, None)
    app.dependency_overrides.pop(get_redis, None)


@pytest.fixture(autouse=True)
def _default_cache_miss():
    # .get.return_value = None is not decorative: a bare MagicMock would
    # return another MagicMock from .get(), which cache_get would read as
    # a hit, fail to deserialize it, and pollute every test in this file
    # with a WARNING. Explicit None is what makes every existing test see
    # a miss and keep calling the provider unchanged.
    _use_cache(_fake_cache())


def _fake_cache():
    cache = MagicMock()
    cache.get.return_value = None
    return cache


def _use_cache(cache):
    app.dependency_overrides[get_redis] = lambda: cache


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _fake_provider(row=None, error=None):
    provider = MagicMock()
    if error is not None:
        provider.get_artist.side_effect = error
    else:
        provider.get_artist.return_value = row
    return provider


def _use_provider(provider):
    app.dependency_overrides[get_search_provider] = lambda: provider


# --- Happy path -------------------------------------------------------------


def test_get_artist_returns_artist_mapped_field_by_field():
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "id": _ARTIST_ID,
        "name": "Main Artist",
        "thumbnail_url": "https://example.com/artist-large.jpg",
        "songs": [
            {
                "track_id": "song-1",
                "title": "Song One",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "album": "Album One",
                "album_id": "MPREb_album1",
                "duration_seconds": 200,
                "thumbnail_url": "https://example.com/song-1-large.jpg",
            },
            {
                "track_id": "song-2",
                "title": "Song Two",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "album": "Album One",
                "album_id": "MPREb_album1",
                "duration_seconds": 210,
                "thumbnail_url": "https://example.com/song-2.jpg",
            },
        ],
        "albums": [
            {
                "id": "MPREb_album1",
                "title": "Album One",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "year": "2020",
                "audio_playlist_id": "OLAK5uy_album1",
                "thumbnail_url": "https://example.com/album-large.jpg",
            }
        ],
        "singles": [
            {
                "id": "MPREb_single1",
                "title": "Single One",
                "year": "2019",
                "type": "Single",
                "thumbnail_url": "https://example.com/single.jpg",
            },
            {
                "id": "MPREb_ep1",
                "title": "EP One",
                "year": "2018",
                "type": "EP",
                "thumbnail_url": "https://example.com/ep.jpg",
            },
        ],
        "related": [
            {
                "id": "UC-related-1",
                "name": "Related Artist",
                "thumbnail_url": "https://example.com/related.jpg",
            }
        ],
    }


def test_get_artist_id_is_the_requested_path_param():
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == _ARTIST_ID


def test_get_artist_calls_provider_exactly_once_with_requested_id():
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    provider.get_artist.assert_called_once_with(_ARTIST_ID)


# --- Expected empty sections --------------------------------------------


def test_get_artist_missing_songs_key_returns_empty_list():
    row = {k: v for k, v in _ARTIST_ROW.items() if k != "songs"}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["songs"] == []


def test_get_artist_songs_without_results_key_returns_empty_list():
    row = {**_ARTIST_ROW, "songs": {"browseId": None}}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["songs"] == []


def test_get_artist_missing_albums_key_returns_empty_list():
    row = {k: v for k, v in _ARTIST_ROW.items() if k != "albums"}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["albums"] == []


def test_get_artist_missing_singles_key_returns_empty_list():
    row = {k: v for k, v in _ARTIST_ROW.items() if k != "singles"}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["singles"] == []


def test_get_artist_missing_related_key_returns_empty_list():
    row = {k: v for k, v in _ARTIST_ROW.items() if k != "related"}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["related"] == []


@pytest.mark.parametrize("section", ["albums", "singles", "related"])
def test_get_artist_section_without_results_key_returns_upstream_error(section):
    # Only songs has a documented branch where the provider creates the
    # category without filling it. For the other three a missing "results"
    # is a layout change, and the endpoint owes a 502 rather than a list
    # that is silently empty.
    row = {**_ARTIST_ROW, section: {"browseId": None}}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("section", ["songs", "albums", "singles", "related"])
def test_get_artist_section_that_is_not_a_dict_returns_upstream_error(section):
    # A section that stopped being a dict is a provider failure like any
    # other: 502, not the 500 a .get() chain would produce by raising
    # AttributeError, which core/upstream.py does not translate.
    row = {**_ARTIST_ROW, section: [_ALBUM_ONE]}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# --- Singles/EPs and release fields --------------------------------------


def test_get_artist_singles_mixes_singles_and_eps_in_provider_order():
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    singles = response.json()["data"]["singles"]
    assert [s["type"] for s in singles] == ["Single", "EP"]
    assert [s["id"] for s in singles] == ["MPREb_single1", "MPREb_ep1"]


def test_get_artist_release_without_year_or_type_returns_null():
    single_without_year_type = {
        k: v for k, v in _SINGLE_ONE.items() if k not in {"year", "type"}
    }
    row = {**_ARTIST_ROW, "singles": {"results": [single_without_year_type]}}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    release = response.json()["data"]["singles"][0]
    assert release["year"] is None
    assert release["type"] is None


# --- album/album_id invariant ---------------------------------------------


def test_get_artist_song_with_album_name_and_id_returns_both():
    song = {**_SONG_ONE, "album": {"name": "Some Album", "id": "MPREb_xyz"}}
    row = {**_ARTIST_ROW, "songs": {"results": [song]}}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    mapped_song = response.json()["data"]["songs"][0]
    assert mapped_song["album"] == "Some Album"
    assert mapped_song["album_id"] == "MPREb_xyz"


def test_get_artist_song_with_null_album_returns_both_null():
    song = {**_SONG_ONE, "album": None}
    row = {**_ARTIST_ROW, "songs": {"results": [song]}}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    mapped_song = response.json()["data"]["songs"][0]
    assert mapped_song["album"] is None
    assert mapped_song["album_id"] is None


def test_get_artist_song_with_album_name_but_null_id_returns_both_null():
    song = {**_SONG_ONE, "album": {"name": "Some Album", "id": None}}
    row = {**_ARTIST_ROW, "songs": {"results": [song]}}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    mapped_song = response.json()["data"]["songs"][0]
    assert mapped_song["album"] is None
    assert mapped_song["album_id"] is None


# --- Degraded song / degraded artist ---------------------------------------


def test_get_artist_degraded_song_is_kept_with_nulls_and_empty_artists():
    degraded_song = {
        "videoId": None,
        "title": "Degraded Song",
        "artists": None,
        "album": None,
        "thumbnails": None,
    }
    row = {**_ARTIST_ROW, "songs": {"results": [degraded_song]}}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    mapped_song = response.json()["data"]["songs"][0]
    assert mapped_song["track_id"] is None
    assert mapped_song["artists"] == []
    assert mapped_song["duration_seconds"] is None
    assert mapped_song["thumbnail_url"] is None


def test_get_artist_null_thumbnails_returns_null_thumbnail_url():
    row = {**_ARTIST_ROW, "thumbnails": None}
    provider = _fake_provider(row=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["thumbnail_url"] is None


# --- Invalid input / auth ---------------------------------------------------


def test_get_artist_id_not_matching_pattern_returns_invalid_request():
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get("/artist/some-song-id")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    provider.get_artist.assert_not_called()


def test_get_artist_album_id_shape_returns_invalid_request():
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get("/artist/MPREb_000")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    provider.get_artist.assert_not_called()


def test_get_artist_mpla_prefixed_id_reaches_provider():
    mpla_id = "MPLAUC1111111111111111111111"
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{mpla_id}")

    assert response.status_code == 200
    provider.get_artist.assert_called_once_with(mpla_id)


def test_get_artist_unauthenticated_returns_unauthorized():
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    provider.get_artist.assert_not_called()


def test_get_artist_with_no_id_returns_not_found():
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get("/artist/")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "not_found"}
    provider.get_artist.assert_not_called()


# --- Upstream failures -------------------------------------------------------


def test_get_artist_nonexistent_id_returns_upstream_error_not_not_found():
    provider = _fake_provider(error=KeyError("musicImmersiveHeaderRenderer"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_artist_provider_network_failure_returns_upstream_error():
    provider = _fake_provider(error=requests.exceptions.ConnectionError())
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_artist_provider_timeout_returns_upstream_timeout():
    provider = _fake_provider(error=requests.exceptions.Timeout())
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_artist_provider_server_error_returns_upstream_error():
    provider = _fake_provider(error=PROVIDER_ERRORS[0]("server error"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_artist_provider_value_error_returns_upstream_error():
    provider = _fake_provider(error=ValueError("layout changed"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_artist_provider_index_error_returns_upstream_error():
    provider = _fake_provider(error=IndexError("layout changed"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize(
    "malformed_row",
    [
        {k: v for k, v in _ARTIST_ROW.items() if k != "name"},
        {k: v for k, v in _ARTIST_ROW.items() if k != "thumbnails"},
        {
            **_ARTIST_ROW,
            "songs": {
                "results": [{k: v for k, v in _SONG_ONE.items() if k != "title"}]
            },
        },
    ],
)
def test_get_artist_malformed_row_returns_upstream_error(malformed_row):
    provider = _fake_provider(row=malformed_row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# --- Cache ------------------------------------------------------------------

_CACHED_ARTIST_JSON = {
    "id": _ARTIST_ID,
    "name": "Main Artist",
    "thumbnail_url": "https://example.com/artist-large.jpg",
    "songs": [
        {
            "track_id": "song-1",
            "title": "Song One",
            "artists": [{"id": "artist-1", "name": "Main Artist"}],
            "album": "Album One",
            "album_id": "MPREb_album1",
            "duration_seconds": 200,
            "thumbnail_url": "https://example.com/song-1-large.jpg",
        },
        {
            "track_id": "song-2",
            "title": "Song Two",
            "artists": [{"id": "artist-1", "name": "Main Artist"}],
            "album": "Album One",
            "album_id": "MPREb_album1",
            "duration_seconds": 210,
            "thumbnail_url": "https://example.com/song-2.jpg",
        },
    ],
    "albums": [
        {
            "id": "MPREb_album1",
            "title": "Album One",
            "artists": [{"id": "artist-1", "name": "Main Artist"}],
            "year": "2020",
            "audio_playlist_id": "OLAK5uy_album1",
            "thumbnail_url": "https://example.com/album-large.jpg",
        }
    ],
    "singles": [
        {
            "id": "MPREb_single1",
            "title": "Single One",
            "year": "2019",
            "type": "Single",
            "thumbnail_url": "https://example.com/single.jpg",
        },
        {
            "id": "MPREb_ep1",
            "title": "EP One",
            "year": "2018",
            "type": "EP",
            "thumbnail_url": "https://example.com/ep.jpg",
        },
    ],
    "related": [
        {
            "id": "UC-related-1",
            "name": "Related Artist",
            "thumbnail_url": "https://example.com/related.jpg",
        }
    ],
}


def test_get_artist_cache_hit_returns_cached_body_without_calling_provider():
    cache = _fake_cache()
    cache.get.return_value = json.dumps(_CACHED_ARTIST_JSON).encode()
    _use_cache(cache)
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_ARTIST_JSON
    provider.get_artist.assert_not_called()


def test_get_artist_miss_writes_cache_with_the_12h_ttl_and_the_artist_key():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    cache.set.assert_called_once()
    args, kwargs = cache.set.call_args
    assert args[0] == f"beatly:v1:artist:{_ARTIST_ID}"
    assert json.loads(args[1]) == response.json()["data"]
    assert kwargs == {"ex": 43200}


def test_get_artist_upstream_error_is_never_cached():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_provider(error=KeyError("musicImmersiveHeaderRenderer"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    cache.set.assert_not_called()

    response_again = client.get(f"/artist/{_ARTIST_ID}")
    assert response_again.status_code == 502
    assert provider.get_artist.call_count == 2


def test_get_artist_redis_failure_on_read_falls_back_to_provider():
    cache = _fake_cache()
    cache.get.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_ARTIST_JSON
    assert "reason" not in response.json()


def test_get_artist_redis_failure_on_write_still_returns_200():
    cache = _fake_cache()
    cache.set.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_ARTIST_JSON


def test_get_artist_corrupted_cached_value_falls_back_to_provider_not_502():
    cache = _fake_cache()
    cache.get.return_value = b"{"
    _use_cache(cache)
    provider = _fake_provider(row=_ARTIST_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_ARTIST_JSON
