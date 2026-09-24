# test/routes/test_public.py
#
# Tests for the public share endpoints.
#
# Tested:
# - GET /public/album/{album_id} maps Album to PublicAlbum field by field,
#   dropping audio_playlist_id, other_versions and related_recommendations
# - GET /public/album/{album_id} with audioPlaylistId: null returns 200
#   with tracks: [] (expected empty, same as GET /album/{id})
# - GET /public/album/{album_id} returns 502/504 for a provider failure or
#   timeout, and 502 (never 404) for a well-formed but unknown album_id
# - GET /public/album/{album_id} rejects a malformed prefix with 422
#   before calling the provider
# - GET /public/album/{album_id} responds 200 with no Authorization header
# - GET /public/artist/{artist_id} maps Artist to PublicArtist field by
#   field, dropping `related`, and keeps `singles` whole -- Single, EP and
#   type: null all travel, each with its own `type` (R4)
# - GET /public/artist/{artist_id} with empty sections returns 200 with
#   songs/albums/singles: []
# - GET /public/artist/{artist_id} returns 502/504 for a provider failure
#   or timeout, and 502 (never 404) for a well-formed but unknown
#   artist_id
# - GET /public/artist/{artist_id} rejects a malformed pattern with 422
#   before calling the provider
# - GET /public/artist/{artist_id} responds 200 with no Authorization
#   header
# - GET /public/playlists/{playlist_id} filters explicitly by
#   .eq("id", ...).eq("is_public", True), never delegating to RLS
# - GET /public/playlists/{playlist_id} reads its tracks ordered by
#   order_key, the same read GET /playlists/{id} uses
# - A private playlist and a nonexistent one return byte-for-byte the same
#   404 playlist_not_found -- the one non-negotiable test
# - GET /public/playlists/{playlist_id} strips owner_id, is_public,
#   created_at and updated_at from the response, and each track has no
#   `id` key (the catalog uuid never crosses this boundary)
# - owner is {"type": "user"}; there is no thumbnail_url field at all on
#   this DTO (PublicPlaylist has none -- only PublicGenrePlaylist does)
# - thumbnails comes from get_user_playlist_thumbnails, called with the
#   exact RPC name and args, and get_playlist_thumbnails (the genre RPC)
#   is never called on this path
# - An empty playlist, and a playlist whose tracks all lack a thumbnail,
#   both return thumbnails: [] as a normal 200
# - has_more/track_count report the 1000-track cap correctly
# - A malformed playlist_id is 422 without reaching the database
# - Each of playlist read, tracks read, duration RPC and thumbnails RPC
#   failing/timing out is reported as 502/504
# - A thumbnails RPC row missing "thumbnail_url" is 502, not []
# - GET /public/playlists/{playlist_id} responds 200 with no Authorization
#   header
# - GET /public/genre-playlists/{playlist_id} returns owner {"type": "app"},
#   the curated thumbnail_url from the row, thumbnails from
#   get_playlist_thumbnails (never get_user_playlist_thumbnails), and
#   total_duration_seconds summed in Python over the whole collection (R5)
# - track_count is len(tracks), ignoring genre_playlists.track_count even
#   when they disagree
# - A playlist with no tracks returns 200 with tracks: [],
#   total_duration_seconds: 0, thumbnails: []
# - An unknown playlist_id is 404 playlist_not_found
# - A malformed playlist_id is 422 without reaching the database
# - Each of the metadata read, tracks read and thumbnails RPC
#   failing/timing out is reported as 502/504
# - A thumbnails RPC row missing "thumbnail_url" is 502, not []
# - GET /public/genre-playlists/{playlist_id} responds 200 with no
#   Authorization header
# - GET /public/tracks/{track_id} maps the first item of
#   get_watch_playlist(videoId=track_id, limit=1)["tracks"] to PublicTrack
#   field by field (videoId -> track_id, artists with id, album/album_id,
#   length "5:38" -> duration_seconds 338, thumbnail singular)
# - A music video (videoType: MUSIC_VIDEO_TYPE_OMV) returns 200 with
#   album/album_id null and artists populated -- an expected empty, not an
#   error
# - An album with a name but no id returns album/album_id both null
# - A YTMusicServerError whose get_song probe reports
#   playabilityStatus.status == "ERROR" returns 404 track_not_found
# - The same error with status == "UNPLAYABLE" returns 502, never 404
# - A provider failure/timeout with the probe reporting status == "OK"
#   returns 502/504 as usual
# - An empty tracks list returns 502 upstream_error, never a 200 with nulls
#   or a 404: the provider already confirmed the track exists
# - A first item whose videoId does not match the requested track_id
#   returns 502, never 200 with the other track's metadata
# - An item missing videoId or title returns 502, never 200 with nulls
# - An unparseable "length" returns duration_seconds: null, never a 500
# - A single call to the provider with limit=1, and get_song is never
#   called on the happy path (the probe is lazy)
# - GET /public/tracks/{track_id} responds 200 with no Authorization header
# - GET /public/tracks/abc (no Path pattern) reaches the provider, never a
#   422
# - GET /public/album/{album_id} and GET /public/artist/{artist_id} share
#   their cache entry with GET /album/{id} and GET /artist/{id}: a hit
#   under the album/artist key returns the reduced Public shape without
#   calling the provider, and a miss writes under that same key with that
#   same TTL (86400/43200)
# - GET /public/tracks/{track_id} has its own cache entry (the "track"
#   key, distinct from "upnext"/"lyrics"/"related"/"credits"), caching
#   TrackRef; a hit returns it without calling the provider and a miss
#   writes it with ex=86400
# - A 404 and the videoId-identity-mismatch 502 on
#   GET /public/tracks/{track_id} are never cached: the write only runs
#   after the identity check passes, never from inside _fetch_track()
# - A Redis failure on read or on write on GET /public/tracks/{track_id}
#   still returns 200 with the provider's data, and a cached value that
#   fails to deserialize falls back to the provider, never 502
#
# What is covered:
# - Happy path, expected empty state, the is_public security filter,
#   private-vs-missing indistinguishability, singles unfiltered with type,
#   owner as an object with no owner_id, thumbnails from the correct
#   per-domain RPC, invalid input, unauthenticated access (by design),
#   upstream failure, upstream timeout, the lazy 404 probe and its
#   != "OK" vs == "ERROR" distinction, the watch-playlist identity guard,
#   cache hit/miss/failure and corrupted value on the two provider-backed
#   endpoints, including the shared album/artist cache entry and the
#   never-cached identity-mismatch branch
#
# Run with: pytest test/routes/test_public.py -v
#
# SEE: routes/public.py, services/public_service.py,
#      services/playlist_service.py, services/genre_service.py,
#      services/track_service.py, core/cache.py

import json
from unittest.mock import MagicMock

import httpx
import pytest
import requests
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError
from redis.exceptions import ConnectionError as RedisConnectionError

from app import app
from core.cache import get_redis
from core.database import get_db
from core.search_provider import PROVIDER_ERRORS, get_search_provider

client = TestClient(app, raise_server_exceptions=False)

_ALBUM_ID = "MPREb_0000000000001"
_ARTIST_ID = "UC1111111111111111111111"
_PLAYLIST_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_MISSING_PLAYLIST_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_GENRE_PLAYLIST_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_MISSING_GENRE_PLAYLIST_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_OWNER_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_search_provider, None)
    app.dependency_overrides.pop(get_db, None)
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


def _use_provider(provider):
    app.dependency_overrides[get_search_provider] = lambda: provider


def _use_db(db):
    app.dependency_overrides[get_db] = lambda: db


# --- Album fixtures -----------------------------------------------------

_ALBUM_TRACK_ONE = {
    "videoId": "track-1",
    "title": "Track One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "duration_seconds": 200,
    "isAvailable": True,
    "thumbnails": [{"url": "https://example.com/track-1.jpg"}],
}

_ALBUM_AUDIO_PLAYLIST_ROW = {
    "id": "OLAK5uy_main",
    "tracks": [_ALBUM_TRACK_ONE],
    "trackCount": 1,
}

_OTHER_VERSION = {
    "browseId": "album-ref-1",
    "title": "Album One (Deluxe)",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "year": "2021",
    "audioPlaylistId": "OLAK5uy_deluxe",
    "thumbnails": [{"url": "https://example.com/deluxe.jpg"}],
}

_ALBUM_ROW = {
    "title": "Album One",
    "year": "2020",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "trackCount": 1,
    "duration_seconds": 200,
    "audioPlaylistId": "OLAK5uy_main",
    "thumbnails": [
        {"url": "https://example.com/album-small.jpg"},
        {"url": "https://example.com/album-large.jpg"},
    ],
    "tracks": [_ALBUM_TRACK_ONE],
    "other_versions": [_OTHER_VERSION],
    "related_recommendations": [_OTHER_VERSION],
}


def _fake_album_provider(row=None, error=None, playlist=None, playlist_error=None):
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


# --- Artist fixtures -----------------------------------------------------

_SONG_ONE = {
    "videoId": "song-1",
    "title": "Song One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "album": {"name": "Album One", "id": "MPREb_album1"},
    "duration_seconds": 200,
    "thumbnails": [{"url": "https://example.com/song-1.jpg"}],
}

_ALBUM_ONE = {
    "browseId": "MPREb_album1",
    "title": "Album One",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "year": "2020",
    "audioPlaylistId": "OLAK5uy_album1",
    "thumbnails": [{"url": "https://example.com/album.jpg"}],
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

_UNTYPED_RELEASE = {
    "browseId": "MPREb_untyped1",
    "title": "Mystery Release",
    "thumbnails": [{"url": "https://example.com/mystery.jpg"}],
}

_RELATED_ONE = {
    "browseId": "UC-related-1",
    "title": "Related Artist",
    "thumbnails": [{"url": "https://example.com/related.jpg"}],
}

_ARTIST_ROW = {
    "channelId": "UC-video-channel-does-not-match-request",
    "name": "Main Artist",
    "thumbnails": [{"url": "https://example.com/artist.jpg"}],
    "songs": {"browseId": "some-playlist-id", "results": [_SONG_ONE]},
    "albums": {"browseId": None, "results": [_ALBUM_ONE]},
    "singles": {
        "browseId": None,
        "results": [_SINGLE_ONE, _EP_ONE, _UNTYPED_RELEASE],
    },
    "related": {"browseId": None, "results": [_RELATED_ONE]},
}


def _fake_artist_provider(row=None, error=None):
    provider = MagicMock()
    if error is not None:
        provider.get_artist.side_effect = error
    else:
        provider.get_artist.return_value = row
    return provider


# --- User playlist fixtures ----------------------------------------------

_TRACK_ONE_UUID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

_PLAYLIST_ROW = {
    "id": _PLAYLIST_ID,
    "owner_id": _OWNER_ID,
    "title": "Road trip",
    "description": "Songs for the car",
    "is_public": True,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}

_TRACK_ONE = {
    "id": _TRACK_ONE_UUID,
    "track_id": "t1",
    "title": "Song A",
    "artists": [{"id": "a1", "name": "Artist One"}],
    "album": "Album A",
    "album_id": "album-a",
    "duration_seconds": 180,
    "thumbnail_url": "https://example.com/a.png",
}


def _fake_public_playlist_db(
    playlist_rows=None,
    entry_rows=None,
    track_rows=None,
    total_count=None,
    duration_total=None,
    thumbnails=None,
    thumbnails_rows=None,
    playlist_error=None,
    entries_error=None,
    tracks_error=None,
    duration_error=None,
    thumbnails_error=None,
):
    if playlist_rows is None:
        playlist_rows = [_PLAYLIST_ROW]
    if entry_rows is None:
        entry_rows = []
    if track_rows is None:
        track_rows = []
    if total_count is None:
        total_count = len(entry_rows)
    if thumbnails is None:
        thumbnails = []
    if thumbnails_rows is None:
        thumbnails_rows = [{"thumbnail_url": url} for url in thumbnails]
    if duration_total is None:
        duration_total = sum(row.get("duration_seconds", 0) for row in track_rows)

    db = MagicMock()
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]
        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "playlists":
            query = table_mock.select.return_value.eq.return_value.eq.return_value
            if playlist_error is not None:
                query.execute.side_effect = playlist_error
            else:
                query.execute.return_value = MagicMock(data=playlist_rows)
        elif name == "playlist_tracks":
            query = table_mock.select.return_value.eq.return_value.order.return_value.limit.return_value
            if entries_error is not None:
                query.execute.side_effect = entries_error
            else:
                query.execute.return_value = MagicMock(
                    data=entry_rows, count=total_count
                )
        elif name == "tracks":
            query = table_mock.select.return_value.in_.return_value
            if tracks_error is not None:
                query.execute.side_effect = tracks_error
            else:
                query.execute.return_value = MagicMock(data=track_rows)
        return table_mock

    db.table.side_effect = table_side_effect
    db.tables = tables

    def rpc_side_effect(name, params):
        rpc_mock = MagicMock()
        if name == "get_playlist_duration_total":
            if duration_error is not None:
                rpc_mock.execute.side_effect = duration_error
            else:
                rpc_mock.execute.return_value = MagicMock(data=duration_total)
        elif name == "get_user_playlist_thumbnails":
            if thumbnails_error is not None:
                rpc_mock.execute.side_effect = thumbnails_error
            else:
                rpc_mock.execute.return_value = MagicMock(data=thumbnails_rows)
        return rpc_mock

    db.rpc.side_effect = rpc_side_effect
    return db


# --- Genre playlist fixtures ----------------------------------------------

_GENRE_PLAYLIST_ROW = {
    "id": _GENRE_PLAYLIST_ID,
    "title": "Genre Mix",
    "description": "Curated for the mood",
    "thumbnail_url": "https://example.com/genre-cover.jpg",
    # Deliberately wrong on purpose: the endpoint must report len(tracks),
    # never this stored column.
    "track_count": 99,
    "category": "Mood",
}

_GENRE_TRACK_ONE = {
    "track_id": "t1",
    "title": "Song A",
    "artists": [{"id": "a1", "name": "Artist One"}],
    "album": "Album A",
    "album_id": "album-a",
    "duration_seconds": 180,
    "thumbnail_url": "https://example.com/a.png",
}


def _fake_public_genre_playlist_db(
    playlist_rows=None,
    playlist_track_rows=None,
    track_rows=None,
    thumbnails=None,
    thumbnails_rows=None,
    playlist_error=None,
    playlist_tracks_error=None,
    tracks_error=None,
    thumbnails_error=None,
):
    if playlist_rows is None:
        playlist_rows = [_GENRE_PLAYLIST_ROW]
    if playlist_track_rows is None:
        playlist_track_rows = []
    if track_rows is None:
        track_rows = []
    if thumbnails is None:
        thumbnails = []
    if thumbnails_rows is None:
        thumbnails_rows = [{"thumbnail_url": url} for url in thumbnails]

    db = MagicMock()
    tables = {}

    def table_side_effect(name):
        if name in tables:
            return tables[name]
        table_mock = MagicMock()
        tables[name] = table_mock
        if name == "genre_playlists":
            query = table_mock.select.return_value.eq.return_value
            if playlist_error is not None:
                query.execute.side_effect = playlist_error
            else:
                query.execute.return_value = MagicMock(data=playlist_rows)
        elif name == "genre_playlist_tracks":
            query = table_mock.select.return_value.eq.return_value.order.return_value.order.return_value
            if playlist_tracks_error is not None:
                query.execute.side_effect = playlist_tracks_error
            else:
                query.execute.return_value = MagicMock(data=playlist_track_rows)
        elif name == "tracks":
            query = table_mock.select.return_value.in_.return_value
            if tracks_error is not None:
                query.execute.side_effect = tracks_error
            else:
                query.execute.return_value = MagicMock(data=track_rows)
        return table_mock

    db.table.side_effect = table_side_effect
    db.tables = tables

    def rpc_side_effect(name, params):
        rpc_mock = MagicMock()
        if name == "get_playlist_thumbnails":
            if thumbnails_error is not None:
                rpc_mock.execute.side_effect = thumbnails_error
            else:
                rpc_mock.execute.return_value = MagicMock(data=thumbnails_rows)
        return rpc_mock

    db.rpc.side_effect = rpc_side_effect
    return db


# =========================================================================
# GET /public/album/{album_id}
# =========================================================================


def test_get_public_album_maps_album_field_by_field():
    provider = _fake_album_provider(row=_ALBUM_ROW, playlist=_ALBUM_AUDIO_PLAYLIST_ROW)
    _use_provider(provider)

    response = client.get(f"/public/album/{_ALBUM_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "id": _ALBUM_ID,
        "title": "Album One",
        "year": "2020",
        "artists": [{"id": "artist-1", "name": "Main Artist"}],
        "track_count": 1,
        "duration_seconds": 200,
        "thumbnail_url": "https://example.com/album-large.jpg",
        "tracks": [
            {
                "track_id": "track-1",
                "title": "Track One",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "duration_seconds": 200,
                "is_available": True,
                "track_number": 1,
            }
        ],
    }
    assert "audio_playlist_id" not in body["data"]
    assert "other_versions" not in body["data"]
    assert "related_recommendations" not in body["data"]


def test_get_public_album_without_audio_playlist_id_returns_empty_tracks():
    row = {**_ALBUM_ROW, "audioPlaylistId": None}
    provider = _fake_album_provider(row=row)
    _use_provider(provider)

    response = client.get(f"/public/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["tracks"] == []
    provider.get_playlist.assert_not_called()


def test_get_public_album_provider_failure_returns_upstream_error():
    provider = _fake_album_provider(error=PROVIDER_ERRORS[0]("server error"))
    _use_provider(provider)

    response = client.get(f"/public/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_public_album_provider_timeout_returns_upstream_timeout():
    provider = _fake_album_provider(error=requests.exceptions.Timeout())
    _use_provider(provider)

    response = client.get(f"/public/album/{_ALBUM_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_public_album_nonexistent_id_returns_upstream_error_not_not_found():
    provider = _fake_album_provider(error=KeyError("microformat"))
    _use_provider(provider)

    response = client.get(f"/public/album/{_ALBUM_ID}")

    assert response.status_code == 502
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_public_album_invalid_prefix_returns_invalid_request():
    provider = _fake_album_provider(row=_ALBUM_ROW, playlist=_ALBUM_AUDIO_PLAYLIST_ROW)
    _use_provider(provider)

    response = client.get("/public/album/some-song-id")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    provider.get_album.assert_not_called()


def test_get_public_album_responds_without_authorization_header():
    provider = _fake_album_provider(row=_ALBUM_ROW, playlist=_ALBUM_AUDIO_PLAYLIST_ROW)
    _use_provider(provider)

    response = client.get(f"/public/album/{_ALBUM_ID}")

    assert response.status_code == 200


# =========================================================================
# GET /public/artist/{artist_id}
# =========================================================================


def test_get_public_artist_returns_singles_whole_with_type():
    provider = _fake_artist_provider(row=_ARTIST_ROW)
    _use_provider(provider)

    response = client.get(f"/public/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "id": _ARTIST_ID,
        "name": "Main Artist",
        "thumbnail_url": "https://example.com/artist.jpg",
        "songs": [
            {
                "track_id": "song-1",
                "title": "Song One",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "album": "Album One",
                "album_id": "MPREb_album1",
                "duration_seconds": 200,
                "thumbnail_url": "https://example.com/song-1.jpg",
            }
        ],
        "albums": [
            {
                "id": "MPREb_album1",
                "title": "Album One",
                "artists": [{"id": "artist-1", "name": "Main Artist"}],
                "year": "2020",
                "audio_playlist_id": "OLAK5uy_album1",
                "thumbnail_url": "https://example.com/album.jpg",
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
            {
                "id": "MPREb_untyped1",
                "title": "Mystery Release",
                "year": None,
                "type": None,
                "thumbnail_url": "https://example.com/mystery.jpg",
            },
        ],
    }
    assert "related" not in body["data"]


def test_get_public_artist_empty_sections_returns_empty_lists():
    row = {
        "channelId": "UC-video-channel",
        "name": "Main Artist",
        "thumbnails": [{"url": "https://example.com/artist.jpg"}],
    }
    provider = _fake_artist_provider(row=row)
    _use_provider(provider)

    response = client.get(f"/public/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["songs"] == []
    assert data["albums"] == []
    assert data["singles"] == []


def test_get_public_artist_provider_failure_returns_upstream_error():
    provider = _fake_artist_provider(error=PROVIDER_ERRORS[0]("server error"))
    _use_provider(provider)

    response = client.get(f"/public/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_public_artist_provider_timeout_returns_upstream_timeout():
    provider = _fake_artist_provider(error=requests.exceptions.Timeout())
    _use_provider(provider)

    response = client.get(f"/public/artist/{_ARTIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_public_artist_nonexistent_id_returns_upstream_error_not_not_found():
    provider = _fake_artist_provider(error=KeyError("channelId"))
    _use_provider(provider)

    response = client.get(f"/public/artist/{_ARTIST_ID}")

    assert response.status_code == 502
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_public_artist_invalid_pattern_returns_invalid_request():
    provider = _fake_artist_provider(row=_ARTIST_ROW)
    _use_provider(provider)

    response = client.get("/public/artist/not-an-id")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    provider.get_artist.assert_not_called()


def test_get_public_artist_responds_without_authorization_header():
    provider = _fake_artist_provider(row=_ARTIST_ROW)
    _use_provider(provider)

    response = client.get(f"/public/artist/{_ARTIST_ID}")

    assert response.status_code == 200


# =========================================================================
# GET /public/playlists/{playlist_id}
# =========================================================================


def test_get_public_playlist_happy_path():
    db = _fake_public_playlist_db(
        entry_rows=[{"track_id": _TRACK_ONE_UUID}],
        track_rows=[_TRACK_ONE],
        thumbnails=["https://example.com/a.png"],
    )
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["id"] == _PLAYLIST_ID
    assert data["title"] == "Road trip"
    assert data["description"] == "Songs for the car"
    assert data["owner"] == {"type": "user"}
    assert data["track_count"] == 1
    assert data["has_more"] is False
    assert data["total_duration_seconds"] == 180
    assert data["thumbnails"] == ["https://example.com/a.png"]
    assert data["tracks"] == [
        {
            "track_id": "t1",
            "title": "Song A",
            "artists": [{"id": "a1", "name": "Artist One"}],
            "album": "Album A",
            "album_id": "album-a",
            "duration_seconds": 180,
            "thumbnail_url": "https://example.com/a.png",
            "position": 1,
        }
    ]
    assert "id" not in data["tracks"][0]
    assert "owner_id" not in data
    assert "is_public" not in data
    assert "created_at" not in data
    assert "updated_at" not in data
    assert "thumbnail_url" not in data


def test_get_public_playlist_reads_tracks_ordered_by_order_key():
    db = _fake_public_playlist_db(
        entry_rows=[{"track_id": _TRACK_ONE_UUID}],
        track_rows=[_TRACK_ONE],
    )
    _use_db(db)

    client.get(f"/public/playlists/{_PLAYLIST_ID}")

    db.tables[
        "playlist_tracks"
    ].select.return_value.eq.return_value.order.assert_called_once_with("order_key")


def test_get_public_playlist_filters_explicitly_by_is_public():
    db = _fake_public_playlist_db()
    _use_db(db)

    client.get(f"/public/playlists/{_PLAYLIST_ID}")

    playlists_table = db.tables["playlists"]
    playlists_table.select.return_value.eq.assert_called_once_with("id", _PLAYLIST_ID)
    playlists_table.select.return_value.eq.return_value.eq.assert_called_once_with(
        "is_public", True
    )


def test_get_public_playlist_private_and_missing_are_identical():
    private_db = _fake_public_playlist_db(playlist_rows=[])
    _use_db(private_db)
    private_response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    missing_db = _fake_public_playlist_db(playlist_rows=[])
    _use_db(missing_db)
    missing_response = client.get(f"/public/playlists/{_MISSING_PLAYLIST_ID}")

    assert private_response.status_code == missing_response.status_code == 404
    assert (
        private_response.json()
        == missing_response.json()
        == {
            "ok": False,
            "reason": "playlist_not_found",
        }
    )


def test_get_public_playlist_with_no_tracks_returns_empty_state():
    db = _fake_public_playlist_db(entry_rows=[], duration_total=0, thumbnails=[])
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tracks"] == []
    assert data["track_count"] == 0
    assert data["has_more"] is False
    assert data["total_duration_seconds"] == 0
    assert data["thumbnails"] == []
    db.rpc.assert_any_call(
        "get_user_playlist_thumbnails",
        {"playlist_ids": [_PLAYLIST_ID], "limit_per_playlist": 4},
    )


def test_get_public_playlist_malformed_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)

    response = client.get("/public/playlists/not-a-uuid")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_get_public_playlist_over_the_cap_reports_has_more():
    db = _fake_public_playlist_db(
        entry_rows=[{"track_id": _TRACK_ONE_UUID}],
        track_rows=[_TRACK_ONE],
        total_count=1500,
    )
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    data = response.json()["data"]
    assert data["track_count"] == 1500
    assert data["has_more"] is True
    assert len(data["tracks"]) == 1


def test_get_public_playlist_responds_without_authorization_header():
    db = _fake_public_playlist_db()
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"playlist_error": APIError({"message": "connection refused"})},
        {"entries_error": APIError({"message": "connection refused"})},
        {"tracks_error": APIError({"message": "connection refused"})},
        {"duration_error": APIError({"message": "connection refused"})},
        {"thumbnails_error": APIError({"message": "connection refused"})},
    ],
)
def test_get_public_playlist_upstream_failure_returns_upstream_error(kwargs):
    db = _fake_public_playlist_db(
        entry_rows=[{"track_id": _TRACK_ONE_UUID}],
        track_rows=[_TRACK_ONE],
        **kwargs,
    )
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"playlist_error": httpx.ReadTimeout("timed out")},
        {"entries_error": httpx.ReadTimeout("timed out")},
        {"tracks_error": httpx.ReadTimeout("timed out")},
        {"duration_error": httpx.ReadTimeout("timed out")},
        {"thumbnails_error": httpx.ReadTimeout("timed out")},
    ],
)
def test_get_public_playlist_upstream_timeout_returns_upstream_timeout(kwargs):
    db = _fake_public_playlist_db(
        entry_rows=[{"track_id": _TRACK_ONE_UUID}],
        track_rows=[_TRACK_ONE],
        **kwargs,
    )
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_public_playlist_thumbnails_row_missing_key_returns_upstream_error():
    db = _fake_public_playlist_db(
        entry_rows=[{"track_id": _TRACK_ONE_UUID}],
        track_rows=[_TRACK_ONE],
        thumbnails_rows=[{"not_thumbnail_url": "x"}],
    )
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# --- The RPC cross-check: the mapping table in plan-114-thumbnails.md ----


def test_get_public_playlist_calls_the_user_rpc_by_exact_name_and_args():
    db = _fake_public_playlist_db(
        entry_rows=[{"track_id": _TRACK_ONE_UUID}],
        track_rows=[_TRACK_ONE],
        thumbnails=["https://example.com/a.png"],
    )
    _use_db(db)

    client.get(f"/public/playlists/{_PLAYLIST_ID}")

    db.rpc.assert_any_call(
        "get_user_playlist_thumbnails",
        {"playlist_ids": [_PLAYLIST_ID], "limit_per_playlist": 4},
    )
    called_names = [call.args[0] for call in db.rpc.call_args_list]
    assert "get_playlist_thumbnails" not in called_names


def test_get_public_playlist_tracks_with_no_thumbnails_returns_empty_mosaic():
    db = _fake_public_playlist_db(
        entry_rows=[{"track_id": _TRACK_ONE_UUID}],
        track_rows=[_TRACK_ONE],
        thumbnails=[],
    )
    _use_db(db)

    response = client.get(f"/public/playlists/{_PLAYLIST_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["thumbnails"] == []
    assert len(data["tracks"]) == 1


# =========================================================================
# GET /public/genre-playlists/{playlist_id}
# =========================================================================


def test_get_public_genre_playlist_happy_path():
    db = _fake_public_genre_playlist_db(
        playlist_track_rows=[{"id": "pt1", "track_id": "t1", "position": 1}],
        track_rows=[_GENRE_TRACK_ONE],
        thumbnails=["https://example.com/a.png"],
    )
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["id"] == _GENRE_PLAYLIST_ID
    assert data["title"] == "Genre Mix"
    assert data["owner"] == {"type": "app"}
    assert data["thumbnail_url"] == "https://example.com/genre-cover.jpg"
    assert data["thumbnails"] == ["https://example.com/a.png"]
    assert data["track_count"] == 1
    assert data["has_more"] is False
    assert data["total_duration_seconds"] == 180
    assert data["tracks"] == [
        {
            "track_id": "t1",
            "title": "Song A",
            "artists": [{"id": "a1", "name": "Artist One"}],
            "album": "Album A",
            "album_id": "album-a",
            "duration_seconds": 180,
            "thumbnail_url": "https://example.com/a.png",
            "position": 1,
        }
    ]


def test_get_public_genre_playlist_track_count_ignores_stored_column():
    db = _fake_public_genre_playlist_db(
        playlist_track_rows=[
            {"id": "pt1", "track_id": "t1", "position": 1},
            {"id": "pt2", "track_id": "t2", "position": 2},
        ],
        track_rows=[_GENRE_TRACK_ONE, {**_GENRE_TRACK_ONE, "track_id": "t2"}],
    )
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    # The row's stored track_count is 99; the real track list has 2.
    assert response.json()["data"]["track_count"] == 2


def test_get_public_genre_playlist_with_no_tracks_returns_empty_state():
    db = _fake_public_genre_playlist_db(playlist_track_rows=[])
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tracks"] == []
    assert data["track_count"] == 0
    assert data["total_duration_seconds"] == 0
    assert data["thumbnails"] == []
    db.rpc.assert_any_call(
        "get_playlist_thumbnails",
        {"playlist_ids": [_GENRE_PLAYLIST_ID], "limit_per_playlist": 4},
    )


def test_get_public_genre_playlist_unknown_id_returns_playlist_not_found():
    db = _fake_public_genre_playlist_db(playlist_rows=[])
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_MISSING_GENRE_PLAYLIST_ID}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "playlist_not_found"}


def test_get_public_genre_playlist_malformed_id_returns_invalid_request():
    db = MagicMock()
    _use_db(db)

    response = client.get("/public/genre-playlists/not-a-uuid")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    db.table.assert_not_called()


def test_get_public_genre_playlist_responds_without_authorization_header():
    db = _fake_public_genre_playlist_db()
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"playlist_error": APIError({"message": "connection refused"})},
        {"playlist_tracks_error": APIError({"message": "connection refused"})},
        {"tracks_error": APIError({"message": "connection refused"})},
        {"thumbnails_error": APIError({"message": "connection refused"})},
    ],
)
def test_get_public_genre_playlist_upstream_failure_returns_upstream_error(kwargs):
    db = _fake_public_genre_playlist_db(
        playlist_track_rows=[{"id": "pt1", "track_id": "t1", "position": 1}],
        track_rows=[_GENRE_TRACK_ONE],
        **kwargs,
    )
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"playlist_error": httpx.ReadTimeout("timed out")},
        {"playlist_tracks_error": httpx.ReadTimeout("timed out")},
        {"tracks_error": httpx.ReadTimeout("timed out")},
        {"thumbnails_error": httpx.ReadTimeout("timed out")},
    ],
)
def test_get_public_genre_playlist_upstream_timeout_returns_upstream_timeout(kwargs):
    db = _fake_public_genre_playlist_db(
        playlist_track_rows=[{"id": "pt1", "track_id": "t1", "position": 1}],
        track_rows=[_GENRE_TRACK_ONE],
        **kwargs,
    )
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_public_genre_playlist_thumbnails_row_missing_key_returns_upstream_error():
    db = _fake_public_genre_playlist_db(
        playlist_track_rows=[{"id": "pt1", "track_id": "t1", "position": 1}],
        track_rows=[_GENRE_TRACK_ONE],
        thumbnails_rows=[{"not_thumbnail_url": "x"}],
    )
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# --- The RPC cross-check: the mapping table in plan-114-thumbnails.md ----


def test_get_public_genre_playlist_calls_the_genre_rpc_by_exact_name_and_args():
    db = _fake_public_genre_playlist_db(
        playlist_track_rows=[{"id": "pt1", "track_id": "t1", "position": 1}],
        track_rows=[_GENRE_TRACK_ONE],
        thumbnails=["https://example.com/a.png"],
    )
    _use_db(db)

    client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    db.rpc.assert_any_call(
        "get_playlist_thumbnails",
        {"playlist_ids": [_GENRE_PLAYLIST_ID], "limit_per_playlist": 4},
    )
    called_names = [call.args[0] for call in db.rpc.call_args_list]
    assert "get_user_playlist_thumbnails" not in called_names


def test_get_public_genre_playlist_tracks_with_no_thumbnails_returns_empty_mosaic():
    db = _fake_public_genre_playlist_db(
        playlist_track_rows=[{"id": "pt1", "track_id": "t1", "position": 1}],
        track_rows=[_GENRE_TRACK_ONE],
        thumbnails=[],
    )
    _use_db(db)

    response = client.get(f"/public/genre-playlists/{_GENRE_PLAYLIST_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["thumbnails"] == []
    assert len(data["tracks"]) == 1


# =========================================================================
# GET /public/tracks/{track_id}
# =========================================================================

_TRACK_ID = "dQw4w9WgXcQ"

_TRACK_WATCH_ITEM = {
    # "thumbnail" singular and "length", not duration_seconds: what
    # get_watch_playlist(...)["tracks"][0] measured live on 2026-09-14.
    "videoId": _TRACK_ID,
    "title": "Never Gonna Give You Up",
    "artists": [{"id": "artist-1", "name": "Rick Astley"}],
    "album": {"name": "Whenever You Need Somebody", "id": "MPREb_track1"},
    "length": "5:38",
    "thumbnail": [
        {"url": "https://example.com/track-small.jpg"},
        {"url": "https://example.com/track-large.jpg"},
    ],
}

_TRACK_WATCH_ROW = {
    "tracks": [_TRACK_WATCH_ITEM],
    "playlistId": f"RDAMVM{_TRACK_ID}",
    "lyrics": "MPLYt_lyrics_browse_id",
    "related": "MPTRt_related_browse_id",
}


def _fake_track_provider(watch=None, error=None, song=None, song_error=None):
    provider = MagicMock()
    if error is not None:
        provider.get_watch_playlist.side_effect = error
    else:
        provider.get_watch_playlist.return_value = watch
    if song_error is not None:
        provider.get_song.side_effect = song_error
    else:
        provider.get_song.return_value = song
    return provider


def test_get_public_track_maps_watch_track_field_by_field():
    provider = _fake_track_provider(watch=_TRACK_WATCH_ROW)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "track_id": _TRACK_ID,
        "title": "Never Gonna Give You Up",
        "artists": [{"id": "artist-1", "name": "Rick Astley"}],
        "album": "Whenever You Need Somebody",
        "album_id": "MPREb_track1",
        "duration_seconds": 338,
        "thumbnail_url": "https://example.com/track-large.jpg",
    }


def test_get_public_track_music_video_has_no_album():
    item = {
        **_TRACK_WATCH_ITEM,
        "album": None,
        "videoType": "MUSIC_VIDEO_TYPE_OMV",
    }
    row = {**_TRACK_WATCH_ROW, "tracks": [item]}
    provider = _fake_track_provider(watch=row)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["album"] is None
    assert data["album_id"] is None
    assert data["artists"] == [{"id": "artist-1", "name": "Rick Astley"}]


def test_get_public_track_album_with_name_but_no_id_returns_both_null():
    item = {**_TRACK_WATCH_ITEM, "album": {"name": "Some Album", "id": None}}
    row = {**_TRACK_WATCH_ROW, "tracks": [item]}
    provider = _fake_track_provider(watch=row)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["album"] is None
    assert data["album_id"] is None


def test_get_public_track_missing_track_status_error_returns_track_not_found():
    provider = _fake_track_provider(
        error=PROVIDER_ERRORS[0]("No content returned by the server."),
        song={"playabilityStatus": {"status": "ERROR"}},
    )
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "track_not_found"}
    provider.get_song.assert_called_once_with(_TRACK_ID)


def test_get_public_track_unplayable_status_returns_upstream_error_not_not_found():
    provider = _fake_track_provider(
        error=PROVIDER_ERRORS[0]("Sign in to confirm your age."),
        song={"playabilityStatus": {"status": "UNPLAYABLE"}},
    )
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 502
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_public_track_provider_failure_with_probe_ok_returns_upstream_error():
    provider = _fake_track_provider(
        error=PROVIDER_ERRORS[0]("Unexpected server response."),
        song={"playabilityStatus": {"status": "OK"}},
    )
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_public_track_provider_timeout_returns_upstream_timeout():
    provider = _fake_track_provider(error=requests.exceptions.Timeout())
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_public_track_empty_tracks_returns_upstream_error():
    # The provider already confirmed track_id exists (no watch_error was
    # raised): a queue that comes back empty is an upstream anomaly, not
    # the track not existing (that is the 404 case above) and not a 200
    # with every field null (that would invent a response).
    row = {**_TRACK_WATCH_ROW, "tracks": []}
    provider = _fake_track_provider(watch=row)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_public_track_identity_mismatch_returns_upstream_error():
    item = {**_TRACK_WATCH_ITEM, "videoId": "otro-track"}
    row = {**_TRACK_WATCH_ROW, "tracks": [item]}
    provider = _fake_track_provider(watch=row)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("missing_key", ["videoId", "title"])
def test_get_public_track_item_missing_required_key_returns_upstream_error(
    missing_key,
):
    item = {k: v for k, v in _TRACK_WATCH_ITEM.items() if k != missing_key}
    row = {**_TRACK_WATCH_ROW, "tracks": [item]}
    provider = _fake_track_provider(watch=row)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_public_track_unparseable_length_returns_null_duration():
    item = {**_TRACK_WATCH_ITEM, "length": "3m07"}
    row = {**_TRACK_WATCH_ROW, "tracks": [item]}
    provider = _fake_track_provider(watch=row)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["duration_seconds"] is None


def test_get_public_track_single_call_and_lazy_probe_not_called():
    provider = _fake_track_provider(watch=_TRACK_WATCH_ROW)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    provider.get_watch_playlist.assert_called_once_with(videoId=_TRACK_ID, limit=1)
    provider.get_song.assert_not_called()


def test_get_public_track_responds_without_authorization_header():
    provider = _fake_track_provider(watch=_TRACK_WATCH_ROW)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200


def test_get_public_track_any_id_reaches_the_provider_no_422():
    provider = _fake_track_provider(
        error=PROVIDER_ERRORS[0]("server error"),
        song={"playabilityStatus": {"status": "ERROR"}},
    )
    _use_provider(provider)

    response = client.get("/public/tracks/abc")

    assert response.status_code != 422
    provider.get_watch_playlist.assert_called_once_with(videoId="abc", limit=1)


# =========================================================================
# Cache
# =========================================================================

_CACHED_FULL_ALBUM_JSON = {
    "id": _ALBUM_ID,
    "title": "Album One",
    "year": "2020",
    "artists": [{"id": "artist-1", "name": "Main Artist"}],
    "track_count": 1,
    "duration_seconds": 200,
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
        }
    ],
    "other_versions": [],
    "related_recommendations": [],
}

# The reduced PublicAlbum shape get_public_album() maps the cached Album
# to: audio_playlist_id, other_versions and related_recommendations are
# dropped, on a hit exactly as on a miss.
_EXPECTED_PUBLIC_ALBUM_DATA = {
    k: v
    for k, v in _CACHED_FULL_ALBUM_JSON.items()
    if k not in {"audio_playlist_id", "other_versions", "related_recommendations"}
}

_CACHED_FULL_ARTIST_JSON = {
    "id": _ARTIST_ID,
    "name": "Main Artist",
    "thumbnail_url": "https://example.com/artist.jpg",
    "songs": [
        {
            "track_id": "song-1",
            "title": "Song One",
            "artists": [{"id": "artist-1", "name": "Main Artist"}],
            "album": "Album One",
            "album_id": "MPREb_album1",
            "duration_seconds": 200,
            "thumbnail_url": "https://example.com/song-1.jpg",
        }
    ],
    "albums": [
        {
            "id": "MPREb_album1",
            "title": "Album One",
            "artists": [{"id": "artist-1", "name": "Main Artist"}],
            "year": "2020",
            "audio_playlist_id": "OLAK5uy_album1",
            "thumbnail_url": "https://example.com/album.jpg",
        }
    ],
    "singles": [
        {
            "id": "MPREb_single1",
            "title": "Single One",
            "year": "2019",
            "type": "Single",
            "thumbnail_url": "https://example.com/single.jpg",
        }
    ],
    "related": [
        {
            "id": "UC-related-1",
            "name": "Related Artist",
            "thumbnail_url": "https://example.com/related.jpg",
        }
    ],
}

_EXPECTED_PUBLIC_ARTIST_DATA = {
    k: v for k, v in _CACHED_FULL_ARTIST_JSON.items() if k != "related"
}

_CACHED_TRACKREF_JSON = {
    "track_id": _TRACK_ID,
    "title": "Never Gonna Give You Up",
    "artists": [{"id": "artist-1", "name": "Rick Astley"}],
    "album": "Whenever You Need Somebody",
    "album_id": "MPREb_track1",
    "duration_seconds": 338,
    "thumbnail_url": "https://example.com/track-large.jpg",
}


# --- /public/album: shares the /album cache entry ----------------------


def test_get_public_album_cache_hit_does_not_call_provider():
    cache = _fake_cache()
    cache.get.return_value = json.dumps(_CACHED_FULL_ALBUM_JSON).encode()
    _use_cache(cache)
    provider = _fake_album_provider()
    _use_provider(provider)

    response = client.get(f"/public/album/{_ALBUM_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _EXPECTED_PUBLIC_ALBUM_DATA
    provider.get_album.assert_not_called()
    provider.get_playlist.assert_not_called()


def test_get_public_album_miss_writes_the_shared_album_key_and_ttl():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_album_provider(row=_ALBUM_ROW, playlist=_ALBUM_AUDIO_PLAYLIST_ROW)
    _use_provider(provider)

    response = client.get(f"/public/album/{_ALBUM_ID}")

    assert response.status_code == 200
    cache.set.assert_called_once()
    args, kwargs = cache.set.call_args
    assert args[0] == f"beatly:v1:album:{_ALBUM_ID}"
    # The write is the full Album shape get_album() caches, not the
    # reduced PublicAlbum response this endpoint maps it down to: dropping
    # the three PublicAlbum omits from the cached payload must reproduce
    # the response body exactly.
    cached = json.loads(args[1])
    reduced = {
        k: v
        for k, v in cached.items()
        if k not in {"audio_playlist_id", "other_versions", "related_recommendations"}
    }
    assert reduced == response.json()["data"]
    assert kwargs == {"ex": 86400}


# --- /public/artist: shares the /artist cache entry ----------------------


def test_get_public_artist_cache_hit_does_not_call_provider():
    cache = _fake_cache()
    cache.get.return_value = json.dumps(_CACHED_FULL_ARTIST_JSON).encode()
    _use_cache(cache)
    provider = _fake_artist_provider()
    _use_provider(provider)

    response = client.get(f"/public/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _EXPECTED_PUBLIC_ARTIST_DATA
    provider.get_artist.assert_not_called()


def test_get_public_artist_miss_writes_the_shared_artist_key_and_ttl():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_artist_provider(row=_ARTIST_ROW)
    _use_provider(provider)

    response = client.get(f"/public/artist/{_ARTIST_ID}")

    assert response.status_code == 200
    cache.set.assert_called_once()
    args, kwargs = cache.set.call_args
    assert args[0] == f"beatly:v1:artist:{_ARTIST_ID}"
    # The write is the full Artist shape get_artist() caches, not the
    # reduced PublicArtist response this endpoint maps it down to: dropping
    # "related" from the cached payload must reproduce the response body.
    cached = json.loads(args[1])
    reduced = {k: v for k, v in cached.items() if k != "related"}
    assert reduced == response.json()["data"]
    assert kwargs == {"ex": 43200}


# --- /public/tracks: its own cache entry, never shared ------------------


def test_get_public_track_cache_hit_returns_trackref_without_calling_provider():
    cache = _fake_cache()
    cache.get.return_value = json.dumps(_CACHED_TRACKREF_JSON).encode()
    _use_cache(cache)
    provider = _fake_track_provider()
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_TRACKREF_JSON
    provider.get_watch_playlist.assert_not_called()
    provider.get_song.assert_not_called()


def test_get_public_track_miss_writes_cache_with_the_24h_ttl_and_the_track_key():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_track_provider(watch=_TRACK_WATCH_ROW)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    cache.set.assert_called_once()
    args, kwargs = cache.set.call_args
    assert args[0] == f"beatly:v1:track:{_TRACK_ID}"
    assert json.loads(args[1]) == response.json()["data"]
    assert kwargs == {"ex": 86400}


def test_get_public_track_not_found_is_never_cached():
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_track_provider(
        error=PROVIDER_ERRORS[0]("No content returned by the server."),
        song={"playabilityStatus": {"status": "ERROR"}},
    )
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 404
    cache.set.assert_not_called()


def test_get_public_track_identity_mismatch_is_never_cached():
    # The one 502 that fires *after* the provider already answered with
    # data in hand: get_track()'s cache_set has to run only after this
    # check, in the wrapper, never inside _fetch_track() before it -- a
    # cache_set placed before the identity check would grab the wrong
    # track's metadata and serve it under the requested id for 24h.
    item = {**_TRACK_WATCH_ITEM, "videoId": "otro-track"}
    row = {**_TRACK_WATCH_ROW, "tracks": [item]}
    cache = _fake_cache()
    _use_cache(cache)
    provider = _fake_track_provider(watch=row)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 502
    cache.set.assert_not_called()


def test_get_public_track_redis_failure_on_read_falls_back_to_provider():
    cache = _fake_cache()
    cache.get.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    provider = _fake_track_provider(watch=_TRACK_WATCH_ROW)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_TRACKREF_JSON
    assert "reason" not in response.json()


def test_get_public_track_redis_failure_on_write_still_returns_200():
    cache = _fake_cache()
    cache.set.side_effect = RedisConnectionError("refused")
    _use_cache(cache)
    provider = _fake_track_provider(watch=_TRACK_WATCH_ROW)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_TRACKREF_JSON


def test_get_public_track_corrupted_cached_value_falls_back_to_provider_not_502():
    cache = _fake_cache()
    cache.get.return_value = b"{"
    _use_cache(cache)
    provider = _fake_track_provider(watch=_TRACK_WATCH_ROW)
    _use_provider(provider)

    response = client.get(f"/public/tracks/{_TRACK_ID}")

    assert response.status_code == 200
    assert response.json()["data"] == _CACHED_TRACKREF_JSON
