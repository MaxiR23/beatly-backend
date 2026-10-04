# test/routes/test_search.py
#
# Tests for the search endpoint.
#
# Tested:
# - GET /search returns artist, songs and albums mapped field by field
#   from the three filtered calls to the external provider (browseId ->
#   id, artist -> name, videoId -> track_id, album.name/album.id,
#   largest thumbnail, also on the artist); an album carries exactly
#   id, title, artists, year and thumbnail_url, even when the provider
#   row has a playlistId
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
# - An album without playlistId (absent or null) is returned, and its
#   playlist_id is not part of the response
# - A cached album that still has playlist_id is a hit, served without it
# - An album without year or without thumbnails has year or
#   thumbnail_url null
# - A song without album (absent, null, no id, not an object) or with a
#   required field missing or null is skipped and logged; the rest of the
#   results are returned with 200
# - An album or an artist with a required field missing is skipped and
#   logged (a skipped artist is data.artist: null), without breaking the
#   primary artist ordering
# - A field of the wrong type (as pydantic rejects it) skips and logs the
#   row, also for nullable fields; the log never carries q or the value
# - A row that is not an object is skipped and logged
# - Every song, or every album, the provider returned being dropped
#   returns 502 upstream_error and caches nothing, per list; a dropped
#   artist never does
# - A provider payload that is not a list returns 502 upstream_error
#   and caches nothing
# - A provider answer with no rows is a cached 200 with empty lists
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
# - An artist row with thumbnails null or [] gives thumbnail_url: null
# - A cache hit returns the cached body without calling the provider
# - A cached artist without thumbnail_url (cached before the field
#   existed) is still a hit, with thumbnail_url: null
# - The artist, song and album thumbnail_url are requested at 544 x 544
#   with smart crop, and a cache miss writes the rewritten URLs
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
# - Cache-Control: a hit sends max-age with the remaining TTL (1234, not
#   3600), a miss (also with no results, or with a failed cache write or
#   read) sends the full 3600, a failed or negative (-1, -2) TTL read on
#   a hit and an upstream error send no-store
#
# What is covered:
# - Happy path, filtered calls, ordering with and without a primary
#   artist, expected empty state, invalid input, unauthenticated
#   access, upstream failure, upstream timeout, partial-failure
#   abort, malformed upstream data, nullable artist ids, results with
#   no artists listed, thumbnails at 544 x 544 (smart crop), cache hit/miss/failure, corrupted value and key
#   normalization, per-row defects (missing or wrong-typed) skipped and
#   logged
#
# Run with: pytest test/routes/test_search.py -v
#
# SEE: routes/search.py, services/search_service.py, core/search_provider.py,
# core/cache.py

import json
import logging
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
    "thumbnails": [
        {"url": "https://example.com/artist-1-small.jpg"},
        {"url": "https://example.com/artist-1-large.jpg"},
    ],
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


# Deliberately not equal to any operation's full TTL, so a test can tell the
# remaining TTL from the full one.
_REMAINING_TTL = 1234


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
    # Same reason as .get above: a bare MagicMock would return another
    # MagicMock from .ttl(), and every cache hit would end in a 500.
    cache.ttl.return_value = _REMAINING_TTL
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


@pytest.mark.parametrize("thumbnails", [None, []])
def test_search_artist_without_thumbnails_has_null_thumbnail_url(thumbnails):
    provider = _fake_provider(
        artists=[{**_ARTIST_ROW, "thumbnails": thumbnails}],
        songs=[_SONG_ROW],
        albums=[_ALBUM_ROW],
    )
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    artist = response.json()["data"]["artist"]
    assert artist["thumbnail_url"] is None
    assert artist["id"] == "artist-1"


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
        "artist": {
            "id": "artist-1",
            "name": "Main Artist",
            "thumbnail_url": "https://example.com/artist-1-large.jpg",
        },
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


# --- Thumbnails at 544 x 544, smart crop ------------------------------------

_SQUARE = "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"


def _provider_with_suffixed_thumbnails():
    host = "https://lh3.googleusercontent.com/abc"
    return _fake_provider(
        artists=[{**_ARTIST_ROW, "thumbnails": [{"url": f"{host}=w226-h226-l90-rj"}]}],
        songs=[{**_SONG_ROW, "thumbnails": [{"url": f"{host}=w60-h60-l90-rj"}]}],
        albums=[{**_ALBUM_ROW, "thumbnails": [{"url": f"{host}=w226-h226-p-l90-rj"}]}],
    )


def test_search_requests_artist_song_and_album_thumbnails_at_544_smart_crop():
    _use_provider(_provider_with_suffixed_thumbnails())
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artist"]["thumbnail_url"] == _SQUARE
    assert data["songs"][0]["thumbnail_url"] == _SQUARE
    assert data["albums"][0]["thumbnail_url"] == _SQUARE


def test_search_miss_caches_the_rewritten_thumbnail_urls():
    cache = _fake_cache()
    _use_cache(cache)
    _use_provider(_provider_with_suffixed_thumbnails())
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    args, _ = cache.set.call_args
    cached = json.loads(args[1])
    assert cached["artist"]["thumbnail_url"] == _SQUARE
    assert cached["songs"][0]["thumbnail_url"] == _SQUARE
    assert cached["albums"][0]["thumbnail_url"] == _SQUARE


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


# --- Per-row defects (issue #182) -------------------------------------------

_LOGGER = "services.search_service"
_ABSENT = object()


def _with(row, **overrides):
    # _ABSENT removes the key, any other value (including None) sets it.
    result = dict(row)
    for key, value in overrides.items():
        if value is _ABSENT:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def _search_logged(caplog, **provider_rows):
    provider = _fake_provider(**provider_rows)
    _use_provider(provider)
    _use_auth()
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        response = client.get("/search", params={"q": "some query"})
    records = [r for r in caplog.records if r.name == _LOGGER]
    return response, [r.getMessage() for r in records]


@pytest.mark.parametrize("playlist_id", [_ABSENT, None])
def test_search_album_without_playlist_id_is_returned(playlist_id):
    album = _with(_ALBUM_ROW, playlistId=playlist_id)
    _use_provider(_fake_provider(artists=[_ARTIST_ROW], albums=[album]))
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    albums = body["data"]["albums"]
    assert [a["id"] for a in albums] == ["album-browse-1"]
    assert set(albums[0]) == {"id", "title", "artists", "year", "thumbnail_url"}


@pytest.mark.parametrize("year", [_ABSENT, None])
def test_search_album_without_year_has_null_year(year):
    album = _with(_ALBUM_ROW, year=year)
    _use_provider(_fake_provider(albums=[album]))
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"]["albums"] == [
        {
            "id": "album-browse-1",
            "title": "Album One",
            "artists": [{"id": "artist-1", "name": "Main Artist"}],
            "year": None,
            "thumbnail_url": "https://example.com/album-1-large.jpg",
        }
    ]


@pytest.mark.parametrize("thumbnails", [_ABSENT, None, []])
def test_search_album_without_thumbnails_has_null_thumbnail_url(thumbnails):
    album = _with(_ALBUM_ROW, thumbnails=thumbnails)
    _use_provider(_fake_provider(albums=[album]))
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    albums = response.json()["data"]["albums"]
    assert albums[0]["thumbnail_url"] is None
    assert albums[0]["year"] == "2020"


@pytest.mark.parametrize(
    "overrides",
    [
        {"album": _ABSENT},
        {"album": None},
        {"album": {"name": "X", "id": None}},
        {"album": {"name": None, "id": "album-x"}},
        {"album": "not an object"},
    ],
)
def test_search_song_without_album_is_skipped_and_logged(caplog, overrides):
    bad = _with(_SONG_ROW, videoId="song-bad", **overrides)

    response, messages = _search_logged(
        caplog, artists=[], songs=[_SONG_ROW_OTHER, bad, _SONG_ROW]
    )

    assert response.status_code == 200
    songs = response.json()["data"]["songs"]
    assert [s["track_id"] for s in songs] == ["song-2", "song-1"]
    assert len(messages) == 1
    assert "song" in messages[0] and "album" in messages[0]


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"videoId": _ABSENT}, "track_id"),
        ({"videoId": None}, "track_id"),
        ({"title": _ABSENT}, "title"),
        ({"title": None}, "title"),
        ({"duration_seconds": _ABSENT}, "duration_seconds"),
        ({"duration_seconds": None}, "duration_seconds"),
        ({"thumbnails": _ABSENT}, "thumbnail_url"),
        ({"thumbnails": None}, "thumbnail_url"),
        ({"thumbnails": []}, "thumbnail_url"),
    ],
)
def test_search_song_missing_a_required_field_is_skipped_and_logged(
    caplog, overrides, field
):
    bad = _with(_SONG_ROW, **{"videoId": "song-bad", **overrides})

    response, messages = _search_logged(
        caplog, artists=[], songs=[_SONG_ROW_OTHER, bad, _SONG_ROW]
    )

    assert response.status_code == 200
    songs = response.json()["data"]["songs"]
    assert [s["track_id"] for s in songs] == ["song-2", "song-1"]
    assert len(messages) == 1
    assert "song" in messages[0] and field in messages[0]


@pytest.mark.parametrize("shape", [_ABSENT, None])
@pytest.mark.parametrize("key, field", [("browseId", "id"), ("title", "title")])
def test_search_album_missing_a_required_field_is_skipped_and_logged(
    caplog, key, field, shape
):
    bad = _with(_ALBUM_ROW, **{key: shape})

    response, messages = _search_logged(
        caplog, artists=[_ARTIST_ROW], albums=[_ALBUM_ROW_OTHER, bad, _ALBUM_ROW]
    )

    assert response.status_code == 200
    albums = response.json()["data"]["albums"]
    assert [a["id"] for a in albums] == ["album-browse-1", "album-browse-2"]
    assert len(messages) == 1
    assert "album" in messages[0] and field in messages[0]


@pytest.mark.parametrize("shape", [_ABSENT, None])
@pytest.mark.parametrize("key, field", [("browseId", "id"), ("artist", "name")])
def test_search_artist_missing_a_required_field_gives_null_artist(
    caplog, key, field, shape
):
    bad = _with(_ARTIST_ROW, **{key: shape})

    response, messages = _search_logged(
        caplog, artists=[bad], songs=[_SONG_ROW_OTHER, _SONG_ROW]
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artist"] is None
    assert [s["track_id"] for s in data["songs"]] == ["song-2", "song-1"]
    assert len(messages) == 1
    assert "artist" in messages[0] and field in messages[0]


@pytest.mark.parametrize(
    "overrides, field_in_log",
    [
        ({"duration_seconds": "3:20"}, "duration_seconds"),
        ({"title": 5}, "title"),
        ({"videoId": 5}, "track_id"),
        ({"artists": "abc"}, "artists"),
        ({"artists": [{"name": 3}]}, "artists.0.name"),
        ({"album": {"name": 5, "id": "album-x"}}, "album"),
        ({"thumbnails": [{"url": 5}]}, "thumbnail_url"),
        ({"thumbnails": {"url": "x"}}, "thumbnail_url"),
        ({"thumbnails": ["x"]}, "thumbnail_url"),
    ],
)
def test_search_song_with_a_wrong_typed_field_is_skipped_and_logged(
    caplog, overrides, field_in_log
):
    bad = {**_SONG_ROW, "videoId": "song-bad", **overrides}

    response, messages = _search_logged(
        caplog, artists=[], songs=[_SONG_ROW_OTHER, bad, _SONG_ROW]
    )

    assert response.status_code == 200
    songs = response.json()["data"]["songs"]
    assert [s["track_id"] for s in songs] == ["song-2", "song-1"]
    assert len(messages) == 1
    assert "song" in messages[0] and field_in_log in messages[0]
    assert "some query" not in messages[0]
    assert "3:20" not in messages[0]


@pytest.mark.parametrize(
    "overrides, field_in_log",
    [
        ({"year": 2017}, "year"),
        ({"title": 5}, "title"),
        ({"browseId": 5}, "id"),
        ({"artists": {"name": "x"}}, "artists"),
        ({"thumbnails": [{"url": 5}]}, "thumbnail_url"),
        ({"thumbnails": "x"}, "thumbnail_url"),
    ],
)
def test_search_album_with_a_wrong_typed_field_is_skipped_and_logged(
    caplog, overrides, field_in_log
):
    bad = {**_ALBUM_ROW, "browseId": "album-bad", **overrides}

    response, messages = _search_logged(
        caplog, artists=[_ARTIST_ROW], albums=[_ALBUM_ROW_OTHER, bad, _ALBUM_ROW]
    )

    assert response.status_code == 200
    albums = response.json()["data"]["albums"]
    assert [a["id"] for a in albums] == ["album-browse-1", "album-browse-2"]
    assert len(messages) == 1
    assert "album" in messages[0] and field_in_log in messages[0]


@pytest.mark.parametrize(
    "overrides, field_in_log",
    [
        ({"artist": 5}, "name"),
        ({"browseId": 5}, "id"),
        ({"thumbnails": [{"url": 5}]}, "thumbnail_url"),
    ],
)
def test_search_artist_with_a_wrong_typed_field_gives_null_artist(
    caplog, overrides, field_in_log
):
    bad = {**_ARTIST_ROW, **overrides}

    response, messages = _search_logged(
        caplog, artists=[bad], songs=[_SONG_ROW_OTHER, _SONG_ROW]
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artist"] is None
    assert [s["track_id"] for s in data["songs"]] == ["song-2", "song-1"]
    assert len(messages) == 1
    assert "artist" in messages[0] and field_in_log in messages[0]


@pytest.mark.parametrize("garbage", ["garbage", None])
@pytest.mark.parametrize("list_name", ["songs", "albums", "artists"])
def test_search_non_object_row_is_skipped(caplog, list_name, garbage):
    good = {"songs": _SONG_ROW, "albums": _ALBUM_ROW, "artists": _ARTIST_ROW}
    # The artists call is limit 1, so its list is only the non-object.
    rows = [garbage] if list_name == "artists" else [garbage, good[list_name]]
    kwargs = {"songs": [_SONG_ROW], "albums": [_ALBUM_ROW]}
    kwargs[list_name] = rows

    response, messages = _search_logged(
        caplog, **{"artists": [], **kwargs} if list_name != "artists" else kwargs
    )

    assert response.status_code == 200
    data = response.json()["data"]
    if list_name == "artists":
        assert data["artist"] is None
    else:
        assert len(data[list_name]) == 1
    assert len(messages) == 1
    assert "not an object" in messages[0]


@pytest.mark.parametrize("garbage", ["garbage", None, 5])
def test_search_non_object_first_artist_does_not_fall_to_the_second(caplog, garbage):
    response, messages = _search_logged(caplog, artists=[garbage, _ARTIST_ROW])

    assert response.status_code == 200
    assert response.json()["data"]["artist"] is None
    assert len(messages) == 1
    assert "not an object" in messages[0]


def _bad_row(kind, cause):
    base = _SONG_ROW if kind == "songs" else _ALBUM_ROW
    id_key = "videoId" if kind == "songs" else "browseId"
    if cause == "missing":
        return _with(base, **{id_key: _ABSENT})
    if cause == "wrong_type":
        return _with(base, title=5)
    return "garbage"


@pytest.mark.parametrize("row_count", [1, 2])
@pytest.mark.parametrize("cause", ["missing", "wrong_type", "non_object"])
@pytest.mark.parametrize("kind", ["songs", "albums"])
def test_search_every_song_or_album_dropped_returns_upstream_error_and_caches_nothing(
    caplog, kind, cause, row_count
):
    cache = _fake_cache()
    _use_cache(cache)
    other = "albums" if kind == "songs" else "songs"
    good = {"songs": [_SONG_ROW], "albums": [_ALBUM_ROW]}
    rows = {
        other: good[other],
        kind: [_bad_row(kind, cause) for _ in range(row_count)],
    }

    response, messages = _search_logged(caplog, artists=[_ARTIST_ROW], **rows)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    cache.set.assert_not_called()
    assert _cache_control(response) == ["no-store"]
    singular = kind[:-1]
    assert any(singular in m and "dropped" in m for m in messages)


@pytest.mark.parametrize("kind", ["songs", "albums"])
def test_search_every_row_dropped_in_one_list_fails_even_if_the_other_is_empty(
    caplog, kind
):
    cache = _fake_cache()
    _use_cache(cache)

    response, messages = _search_logged(
        caplog, artists=[_ARTIST_ROW], **{kind: [_bad_row(kind, "missing")]}
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    cache.set.assert_not_called()
    assert any("dropped" in m for m in messages)


def test_search_with_no_results_is_cached_as_an_empty_200():
    cache = _fake_cache()
    _use_cache(cache)
    _use_provider(_fake_provider())
    _use_auth()

    response = client.get("/search", params={"q": "nonsense"})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {"artist": None, "songs": [], "albums": []},
    }
    cache.set.assert_called_once()
    assert json.loads(cache.set.call_args.args[1]) == {
        "artist": None,
        "songs": [],
        "albums": [],
    }


@pytest.mark.parametrize("artist_defect", [{"browseId": _ABSENT}, {"artist": 5}])
@pytest.mark.parametrize("with_rows", [True, False])
def test_search_dropped_artist_never_fails_the_search(artist_defect, with_rows):
    cache = _fake_cache()
    _use_cache(cache)
    rows = {"songs": [_SONG_ROW], "albums": [_ALBUM_ROW]} if with_rows else {}
    _use_provider(_fake_provider(artists=[_with(_ARTIST_ROW, **artist_defect)], **rows))
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["artist"] is None
    assert len(data["songs"]) == (1 if with_rows else 0)
    cache.set.assert_called_once()


@pytest.mark.parametrize("list_name", ["artists", "songs", "albums"])
def test_search_non_list_provider_payload_returns_upstream_error(list_name):
    cache = _fake_cache()
    _use_cache(cache)
    _use_provider(_fake_provider(**{list_name: {"videoId": "x"}}))
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    cache.set.assert_not_called()


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
    "artist": {
        "id": "artist-1",
        "name": "Main Artist",
        "thumbnail_url": "https://example.com/artist-1-large.jpg",
    },
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


def test_search_cached_artist_without_thumbnail_url_is_still_a_hit():
    cached = {
        **_CACHED_SEARCH_JSON,
        "artist": {"id": "artist-1", "name": "Main Artist"},
    }
    cache = _fake_cache()
    cache.get.return_value = json.dumps(cached).encode()
    _use_cache(cache)
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"]["artist"]["thumbnail_url"] is None
    provider.search.assert_not_called()


def test_search_cached_album_with_playlist_id_is_still_a_hit():
    cached = {
        **_CACHED_SEARCH_JSON,
        "albums": [{**_CACHED_SEARCH_JSON["albums"][0], "playlist_id": "playlist-1"}],
    }
    cache = _fake_cache()
    cache.get.return_value = json.dumps(cached).encode()
    _use_cache(cache)
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    album = response.json()["data"]["albums"][0]
    assert "playlist_id" not in album
    assert album["id"] == "album-browse-1"
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


# --- Cache-Control -----------------------------------------------------------


def _cache_control(response):
    return response.headers.get_list("cache-control")


def test_search_cache_hit_sends_remaining_ttl_as_max_age():
    cache = _fake_cache()
    cache.get.return_value = json.dumps(_CACHED_SEARCH_JSON).encode()
    _use_cache(cache)
    _use_provider(_fake_provider())
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert _cache_control(response) == ["max-age=1234"]
    cache.ttl.assert_called_once_with(_SOME_QUERY_SEARCH_KEY)


def test_search_miss_sends_full_ttl_as_max_age():
    _use_provider(
        _fake_provider(artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW])
    )
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert _cache_control(response) == ["max-age=3600"]


def test_search_with_no_results_sends_full_ttl_as_max_age():
    _use_provider(_fake_provider())
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"] == {"artist": None, "songs": [], "albums": []}
    assert _cache_control(response) == ["max-age=3600"]


def test_search_upstream_error_sends_no_store():
    _use_provider(
        _fake_provider(errors={"artists": requests.exceptions.ConnectionError()})
    )
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 502
    assert _cache_control(response) == ["no-store"]


def test_search_redis_failure_on_write_sends_full_ttl_as_max_age():
    cache = _fake_cache()
    cache.set.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    _use_provider(
        _fake_provider(artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW])
    )
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_SEARCH_JSON
    assert _cache_control(response) == ["max-age=3600"]


def test_search_redis_failure_on_read_sends_full_ttl_as_max_age():
    cache = _fake_cache()
    cache.get.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    _use_provider(
        _fake_provider(artists=[_ARTIST_ROW], songs=[_SONG_ROW], albums=[_ALBUM_ROW])
    )
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert _cache_control(response) == ["max-age=3600"]


def test_search_ttl_read_failure_on_hit_sends_no_store():
    cache = _fake_cache()
    cache.get.return_value = json.dumps(_CACHED_SEARCH_JSON).encode()
    cache.ttl.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    _use_provider(_fake_provider())
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_SEARCH_JSON
    assert _cache_control(response) == ["no-store"]


@pytest.mark.parametrize("unusable", [-1, -2])
def test_search_unusable_ttl_on_hit_sends_no_store(unusable):
    cache = _fake_cache()
    cache.get.return_value = json.dumps(_CACHED_SEARCH_JSON).encode()
    cache.ttl.return_value = unusable
    _use_cache(cache)
    _use_provider(_fake_provider())
    _use_auth()

    response = client.get("/search", params={"q": "some query"})

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_SEARCH_JSON
    assert _cache_control(response) == ["no-store"]
