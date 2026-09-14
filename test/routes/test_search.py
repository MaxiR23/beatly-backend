# test/routes/test_search.py
#
# Tests for the search endpoint.
#
# Tested:
# - GET /search returns artist, songs and albums mapped field by field
#   from the three filtered calls to the external provider (browseId ->
#   id, artist -> name, videoId -> track_id, album.name/album.id,
#   playlistId, largest thumbnail)
# - The artist call is made with limit 1 and the artists filter; the
#   songs and albums calls carry no limit of their own
# - When artist is not null, songs and albums whose artists include its
#   id come first, preserving relative order within each group, with
#   nothing dropped
# - When artist is null, songs and albums are returned in the order the
#   provider returned them, unreordered
# - No results is 200 ok:true with artist: null and empty lists, never
#   404 or ok:false
# - Missing q returns 422 invalid_request without calling the provider
# - Empty q (?q=) returns 422 invalid_request without calling the
#   provider
# - No token returns 401 unauthorized without calling the provider
# - A network failure from the provider returns 502 upstream_error
# - A timeout from the provider returns 504 upstream_timeout
# - A server error from the provider returns 502 upstream_error
# - A non-JSON response from the provider returns 502 upstream_error
# - A failure in the second of the three calls aborts the whole
#   response with 502, never a mixed response with the two resolved
#   lists
# - A malformed row (missing duration_seconds or null album) returns 502
#   upstream_error, not a 200 with null fields or a 500
# - A song artist with a null id is included, in the second group, and
#   travels as {"id": null, ...} in the response
# - An album whose only artist has a null id falls into the second
#   group, not the first
# - A ValueError raised while parsing the provider's response returns
#   502 upstream_error
# - An IndexError raised while parsing the provider's response returns
#   502 upstream_error
# - A song or an album row with no "artists" key at all, or with
#   "artists": [], returns 200 with artists: [] for that item, not 502
# - A song or an album with no artists listed falls into the second
#   group, without being dropped
# - A cache hit returns the cached body without calling the provider
# - A cache miss writes the response with the hashed search key and
#   ex=3600
# - A 502 is never written to cache: a second request with the same q
#   still calls the provider
# - A Redis failure on read or on write still returns 200 with the
#   provider's data
# - A cached value that fails to deserialize falls back to the provider,
#   never 502
# - "Beatles" and "  beatles  " produce the same cache key; a different
#   q produces a different one; the key never carries the raw query text
#
# What is covered:
# - Happy path, filtered calls, ordering with and without a primary
#   artist, expected empty state, invalid input, unauthenticated
#   access, upstream failure, upstream timeout, partial-failure
#   abort, malformed upstream data, nullable artist ids, results with
#   no artists listed, cache hit/miss/failure, corrupted value and key
#   normalization
#
# Run with: pytest test/routes/test_search.py -v
#
# SEE: routes/search.py, services/search_service.py, core/search_provider.py,
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

_ARTIST_ROW = {
    "browseId": "artist-1",
    "artist": "Main Artist",
}

_SONG_ROW = {
    "videoId": "song-1",
    "title": "Song One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "album": {"name": "Album One", "id": "album-1"},
    "duration_seconds": 200,
    "thumbnails": [
        {"url": "https://example.com/song-1-small.jpg"},
        {"url": "https://example.com/song-1-large.jpg"},
    ],
}

_SONG_ROW_OTHER = {
    "videoId": "song-2",
    "title": "Song Two",
    "artists": [{"id": "artist-2", "name": "Other Artist"}],
    "album": {"name": "Album Two", "id": "album-2"},
    "duration_seconds": 180,
    "thumbnails": [{"url": "https://example.com/song-2.jpg"}],
}

_ALBUM_ROW = {
    "browseId": "album-browse-1",
    "playlistId": "playlist-1",
    "title": "Album One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "year": "2020",
    "thumbnails": [
        {"url": "https://example.com/album-1-small.jpg"},
        {"url": "https://example.com/album-1-large.jpg"},
    ],
}

_ALBUM_ROW_OTHER = {
    "browseId": "album-browse-2",
    "playlistId": "playlist-2",
    "title": "Album Two",
    "artists": [{"id": "artist-2", "name": "Other Artist"}],
    "year": "2021",
    "thumbnails": [{"url": "https://example.com/album-2.jpg"}],
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


def _fake_provider(artists=None, songs=None, albums=None, errors=None):
    errors = errors or {}
    results = {"artists": artists or [], "songs": songs or [], "albums": albums or []}

    def _search(q, filter=None, limit=20):
        if filter not in results:
            raise AssertionError(f"unexpected filter: {filter!r}")
        if filter in errors:
            raise errors[filter]
        return results[filter]

    provider = MagicMock()
    provider.search.side_effect = _search
    return provider


def _use_provider(provider):
    app.dependency_overrides[get_search_provider] = lambda: provider


def _calls_by_filter(provider):
    return {call.kwargs.get("filter"): call for call in provider.search.call_args_list}


# --- Happy path -----------------------------------------------------------


def test_search_returns_artist_songs_and_albums_mapped_field_by_field():
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "artist": {"id": "artist-1", "name": "Main Artist"},
        "songs": [
            {
                "track_id": "song-1",
                "title": "Song One",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "album": "Album One",
                "album_id": "album-1",
                "duration_seconds": 200,
                "thumbnail_url": "https://example.com/song-1-large.jpg",
            }
        ],
        "albums": [
            {
                "id": "album-browse-1",
                "playlist_id": "playlist-1",
                "title": "Album One",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "year": "2020",
                "thumbnail_url": "https://example.com/album-1-large.jpg",
            }
        ],
    }


def test_search_calls_artist_filter_with_limit_one_and_others_without_limit():
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    calls = _calls_by_filter(provider)
    assert calls["artists"].kwargs == {"filter": "artists", "limit": 1}
    assert calls["songs"].kwargs == {"filter": "songs"}
    assert calls["albums"].kwargs == {"filter": "albums"}


def test_search_orders_primary_artist_items_first_without_dropping_any():
    provider = _fake_provider(
        artists=[_ARTIST_ROW],
        songs=[_SONG_ROW_OTHER, _SONG_ROW],
        albums=[_ALBUM_ROW_OTHER, _ALBUM_ROW],
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert [song["track_id"] for song in data["songs"]] == ["song-1", "song-2"]
    assert [album["id"] for album in data["albums"]] == [
        "album-browse-1",
        "album-browse-2",
    ]
    assert len(data["songs"]) == 2
    assert len(data["albums"]) == 2


def test_search_without_matching_artist_keeps_provider_order():
    provider = _fake_provider(
        artists=[], songs=[_SONG_ROW_OTHER, _SONG_ROW], albums=[_ALBUM_ROW_OTHER]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artist"] is None
    assert [song["track_id"] for song in data["songs"]] == ["song-2", "song-1"]
    assert [album["id"] for album in data["albums"]] == ["album-browse-2"]


def test_search_with_no_results_returns_ok_true_with_empty_lists():
    provider = _fake_provider(artists=[], songs=[], albums=[])
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "nonsense"})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {"artist": None, "songs": [], "albums": []},
    }


# --- Invalid input / auth --------------------------------------------------


def test_search_missing_q_returns_invalid_request():
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get("/search")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    provider.search.assert_not_called()


def test_search_empty_q_returns_invalid_request():
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": ""})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    provider.search.assert_not_called()


def test_search_unauthenticated_returns_unauthorized():
    provider = _fake_provider()
    _use_provider(provider)

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    provider.search.assert_not_called()


# --- Upstream failures ------------------------------------------------------


def test_search_provider_network_failure_returns_upstream_error():
    provider = _fake_provider(errors={"artists": requests.exceptions.ConnectionError()})
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_search_provider_timeout_returns_upstream_timeout():
    provider = _fake_provider(errors={"artists": requests.exceptions.ReadTimeout()})
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_search_provider_server_error_returns_upstream_error():
    provider = _fake_provider(errors={"artists": PROVIDER_ERRORS[0]("server error")})
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_search_provider_non_json_response_returns_upstream_error():
    provider = _fake_provider(
        errors={"artists": json.JSONDecodeError("bad json", "doc", 0)}
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_search_provider_value_error_returns_upstream_error():
    provider = _fake_provider(errors={"songs": ValueError("layout changed")})
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_search_provider_index_error_returns_upstream_error():
    provider = _fake_provider(errors={"songs": IndexError("layout changed")})
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_search_second_call_failure_aborts_whole_response():
    provider = _fake_provider(
        artists=[_ARTIST_ROW],
        errors={"songs": requests.exceptions.ConnectionError()},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    calls = _calls_by_filter(provider)
    assert "albums" not in calls


@pytest.mark.parametrize(
    "malformed_song",
    [
        {k: v for k, v in _SONG_ROW.items() if k != "duration_seconds"},
        {**_SONG_ROW, "album": None},
    ],
)
def test_search_malformed_song_row_returns_upstream_error(malformed_song):
    provider = _fake_provider(artists=[], songs=[malformed_song], albums=[])
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# --- Nullable artist id within songs/albums (adenda) ------------------------


def test_search_song_artist_with_null_id_is_kept_in_second_group():
    unlinked_song = {
        **_SONG_ROW_OTHER,
        "videoId": "song-unlinked",
        "artists": [
            {"id": None, "name": "Featuring Someone"},
            {"id": "artist-2", "name": "Other Artist"},
        ],
    }
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW, unlinked_song], albums=[]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert [song["track_id"] for song in data["songs"]] == ["song-1", "song-unlinked"]
    unlinked_result = data["songs"][1]
    assert unlinked_result["artists"][0] == {"id": None, "name": "Featuring Someone"}


def test_search_album_with_only_unlinked_artist_falls_to_second_group():
    unlinked_album = {
        **_ALBUM_ROW_OTHER,
        "browseId": "album-unlinked",
        "artists": [{"id": None, "name": "Featuring Someone"}],
    }
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[], albums=[_ALBUM_ROW, unlinked_album]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert [album["id"] for album in data["albums"]] == [
        "album-browse-1",
        "album-unlinked",
    ]


# --- No artists listed (issue #102) -----------------------------------------


@pytest.mark.parametrize(
    "song_without_artists",
    [
        {k: v for k, v in _SONG_ROW.items() if k != "artists"},
        {**_SONG_ROW, "artists": []},
    ],
)
def test_search_song_with_no_artists_returns_ok_with_empty_artists(
    song_without_artists,
):
    provider = _fake_provider(artists=[], songs=[song_without_artists], albums=[])
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["songs"] == [
        {
            "track_id": "song-1",
            "title": "Song One",
            "artists": [],
            "album": "Album One",
            "album_id": "album-1",
            "duration_seconds": 200,
            "thumbnail_url": "https://example.com/song-1-large.jpg",
        }
    ]


@pytest.mark.parametrize(
    "album_without_artists",
    [
        {k: v for k, v in _ALBUM_ROW.items() if k != "artists"},
        {**_ALBUM_ROW, "artists": []},
    ],
)
def test_search_album_with_no_artists_returns_ok_with_empty_artists(
    album_without_artists,
):
    provider = _fake_provider(artists=[], songs=[], albums=[album_without_artists])
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["albums"] == [
        {
            "id": "album-browse-1",
            "playlist_id": "playlist-1",
            "title": "Album One",
            "artists": [],
            "year": "2020",
            "thumbnail_url": "https://example.com/album-1-large.jpg",
        }
    ]


def test_search_song_without_artists_falls_to_second_group():
    song_without_artists = {k: v for k, v in _SONG_ROW_OTHER.items() if k != "artists"}
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[song_without_artists, _SONG_ROW], albums=[]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert [song["track_id"] for song in data["songs"]] == ["song-1", "song-2"]
    assert len(data["songs"]) == 2


def test_search_album_without_artists_falls_to_second_group():
    album_without_artists = {
        k: v for k, v in _ALBUM_ROW_OTHER.items() if k != "artists"
    }
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[], albums=[album_without_artists, _ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert [album["id"] for album in data["albums"]] == [
        "album-browse-1",
        "album-browse-2",
    ]
    assert len(data["albums"]) == 2


# --- Cache ------------------------------------------------------------------

_CACHED_SEARCH_JSON = {
    "artist": {"id": "artist-1", "name": "Main Artist"},
    "songs": [
        {
            "track_id": "song-1",
            "title": "Song One",
            "artists": [{"id": "artist-1", "name": "Main Artist"}],
            "album": "Album One",
            "album_id": "album-1",
            "duration_seconds": 200,
            "thumbnail_url": "https://example.com/song-1-large.jpg",
        }
    ],
    "albums": [
        {
            "id": "album-browse-1",
            "playlist_id": "playlist-1",
            "title": "Album One",
            "artists": [{"id": "artist-1", "name": "Main Artist"}],
            "year": "2020",
            "thumbnail_url": "https://example.com/album-1-large.jpg",
        }
    ],
}


# Fixed expected value, not recomputed with the same normalization the
# key builder under test uses: a literal is what actually pins the key
# for "some query", the exact string every test below queries with.
_SOME_QUERY_SEARCH_KEY = (
    "beatly:v1:search:2ac0bebb00b8a127cc9d93c7035402e08ca759af5717c22470f69c2b2f072c30"
)


def test_search_cache_hit_returns_cached_body_without_calling_provider():
    cache = _fake_cache()
    cache.get.return_value = json.dumps(_CACHED_SEARCH_JSON).encode()
    _use_cache(cache)
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_SEARCH_JSON
    provider.search.assert_not_called()


def test_search_miss_writes_cache_with_the_1h_ttl_and_the_hashed_key():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    cache.set.assert_called_once()
    args, kwargs = cache.set.call_args
    assert args[0] == _SOME_QUERY_SEARCH_KEY
    assert json.loads(args[1]) == response.json()["data"]
    assert kwargs == {"ex": 3600}


def test_search_upstream_error_is_never_cached():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_provider(errors={"artists": requests.exceptions.ConnectionError()})
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    cache.set.assert_not_called()

    response_again = client.get("/search", params={"q": "some query"})
    assert response_again.status_code == 502
    assert provider.search.call_count > 1


def test_search_redis_failure_on_read_falls_back_to_provider():
    cache = _fake_cache()
    cache.get.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_SEARCH_JSON
    assert "reason" not in response.json()


def test_search_redis_failure_on_write_still_returns_200():
    cache = _fake_cache()
    cache.set.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_SEARCH_JSON


def test_search_corrupted_cached_value_falls_back_to_provider_not_502():
    cache = _fake_cache()
    cache.get.return_value = b"{"
    _use_cache(cache)
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_SEARCH_JSON


def test_search_key_normalizes_case_and_surrounding_whitespace():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    client.get("/search", params={"q": "Beatles"})
    client.get("/search", params={"q": "  beatles  "})

    keys = [call.args[0] for call in cache.get.call_args_list]
    assert keys[0] == keys[1]


def test_search_key_differs_for_a_different_query():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    client.get("/search", params={"q": "Beatles"})
    client.get("/search", params={"q": "other"})

    keys = [call.args[0] for call in cache.get.call_args_list]
    assert keys[0] != keys[1]


def test_search_key_starts_with_prefix_and_ends_with_64_hex_chars():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_provider(
        artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW]
    )
    _use_provider(provider)
    _use_auth()

    client.get("/search", params={"q": "some query"})

    key = cache.get.call_args.args[0]
    assert key.startswith("beatly:v1:search:")
    digest = key.removeprefix("beatly:v1:search:")
    assert len(digest) == 64
    assert "some query" not in key
