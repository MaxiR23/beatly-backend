# test/routes/test_tracks.py
#
# Tests for the tracks endpoints.
#
# Tested:
# - GET /tracks/{track_id}/upnext returns up to 50 tracks mapped field by
#   field from a single call to the external provider (videoId -> track_id,
#   thumbnail from "thumbnail" singular, length "3:07" -> duration_seconds
#   187)
# - An item with an explicit duration_seconds wins over "length"
# - An item whose "length" is in an unexpected format returns
#   duration_seconds: null, never a 500
# - The first item, with no artists and no album, still appears with
#   artists: [] and album/album_id null
# - A single call to the provider (get_watch_playlist(videoId=..., limit=50)),
#   and get_song is never called on the happy path (the probe is lazy)
# - An empty tracks list returns 200 with data.tracks: []
# - A degraded item (no artists, album: None, no length/duration_seconds,
#   thumbnail: None) returns 200 with nulls and artists: []
# - An item missing videoId or title returns 502, never 200 with nulls or 500
# - GET /tracks/{track_id}/lyrics returns has_timestamps/source/lines mapped
#   from timestamped LyricLine objects, in milliseconds
# - Lyrics without timestamps split the provider's \r\n text with
#   splitlines(), leaving no trailing \r, with start_ms/end_ms null on every
#   line
# - A blank line between stanzas is preserved as text: ""
# - watch["lyrics"] being None returns 200 with data.lyrics: null and never
#   calls get_lyrics (the falsy-browse-id cut)
# - get_lyrics returning None returns 200 with data.lyrics: null
# - source: None returns 200 with source: null
# - get_watch_playlist is called once with limit=1, get_lyrics once with the
#   watch playlist's lyrics browse id
# - Timestamped lines arriving as dicts instead of dataclasses return 502
#   (vars() raises TypeError), never 500
# - A lyrics payload missing hasTimestamps returns 502
# - GET /tracks/{track_id}/related concatenates the song-shaped sections,
#   keeping only MUSIC_VIDEO_TYPE_ATV items, in provider order, and maps
#   albums/artists; recommended playlists are absent from the result
# - A non-ATV song item (UGC, OMV, OFFICIAL_SOURCE_MUSIC) never appears in
#   data.songs
# - A song item missing the videoType key never appears in data.songs
# - A section whose contents is a string ("About the artist") and a None
#   item inside a list are both skipped without a 500
# - Sections are identified by the shape of their items, not by title:
#   translating every title still returns the same data
# - An album with audioPlaylistId: None and an artist with subscribers: None
#   are still classified by key presence, not by truth
# - watch["related"] being None returns 200 with the three lists empty and
#   never calls get_song_related
# - get_song_related returning [] returns 200 with the three lists empty
# - Sections present but with no surviving ATV item return data.songs: []
#   with the other two lists populated
# - get_watch_playlist is called once with limit=1, get_song_related once
#   with the watch playlist's related browse id
# - A YTMusicServerError from get_watch_playlist whose get_song probe
#   reports playabilityStatus.status == "ERROR" returns 404
#   track_not_found, on all three endpoints, with get_song called exactly
#   once
# - The same error with status == "UNPLAYABLE" returns 502 upstream_error,
#   never 404 (fixes the D1 rule: != "OK" would wrongly 404 this case)
# - The same error with status == "OK" returns 502
# - The same error with the probe missing the playabilityStatus key returns
#   502, never 404 or 500
# - The same error with the probe itself failing (server error, or a
#   timeout) returns 502 / 504 respectively
# - The 404 body never contains the original exception's message, and its
#   keys are exactly {"ok", "reason"}
# - A network failure, a timeout, a ProviderParseError, a KeyError, a
#   TypeError, a ValueError and an IndexError from the provider each map to
#   502/504 as usual, on all three endpoints, on the first call
#   (get_watch_playlist) -- a failure after the track is confirmed to exist
#   is never a 404
# - The same, plus a provider server error (YTMusicServerError), on the
#   second call (get_lyrics / get_song_related)
# - No token returns 401 unauthorized without calling the provider, on all
#   three endpoints
# - GET /tracks/upnext and GET /tracks//upnext return 404 not_found,
#   ok: false (no route matches; produced by Starlette, not the domain)
# - None of the three endpoints wraps its response in Paginated[T]: data
#   never carries a "page" key
# - GET /tracks/{track_id}/credits builds the browseId as "MPTC" + track_id
#   and makes a single call to get_song_credits, with no get_watch_playlist,
#   no get_album and no get_song on the happy path
# - The four typed sections (performed_by, written_by, produced_by,
#   music_metadata_provided_by) are mapped with data -> names, and an
#   unrecognized section falls into other_sections instead of being dropped
# - A typed section present with data: [] returns names: [] (present, not
#   null); classification is by key presence, never by localized_title
# - A get_song_credits KeyError or IndexError, with the probe confirming the
#   track exists (status OK or UNPLAYABLE), returns 200 with all four typed
#   sections null and other_sections: [] -- never a 404 or a 502
# - The same KeyError, with the probe reporting status == "ERROR", returns
#   404 track_not_found, on par with a YTMusicServerError under the same
#   probe condition: the 404 fires from either failure path
# - A genuine provider failure (server error, timeout, ValueError,
#   TypeError) never returns 404 or an empty 200
# - The probe itself failing (server error, timeout, or a response missing
#   playabilityStatus) returns 502/504 instead of the empty 200, proving the
#   empty branch requires positive confirmation, not just a caught KeyError
# - A malformed credits payload (section missing localized_title, response
#   missing other_sections, names arriving as a string) returns 502, never
#   a 200 empty or a 500
# - No token returns 401 without calling get_song_credits; data never
#   carries a "page" key
#
# What is covered:
# - Happy path, single-call contract, expected empty states (by a falsy
#   browse id and by an empty provider response), the lazy 404 probe and
#   its != "OK" vs == "ERROR" distinction, malformed upstream data,
#   unauthenticated access, upstream failure, upstream timeout, no-route
#   404, the credits navigation-failure branch and its own probe
#
# Run with: pytest test/routes/test_tracks.py -v
#
# SEE: routes/tracks.py, services/track_service.py, core/search_provider.py

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

from app import app
from core.auth import get_current_user_id
from core.search_provider import PROVIDER_ERRORS, get_search_provider

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_TRACK_ID = "dQw4w9WgXcQ"

# --- /upnext fixtures --------------------------------------------------------

_UPNEXT_CURRENT = {
    # The first item of the queue: the track being played, with no artists
    # and no album, and "thumbnail" (singular) like every watch track.
    "videoId": "current-track",
    "title": "Current Track",
    "length": "3:07",
    "thumbnail": [
        {"url": "https://example.com/current-small.jpg"},
        {"url": "https://example.com/current-large.jpg"},
    ],
}

_UPNEXT_SONG_TWO = {
    "videoId": "song-2",
    "title": "Song Two",
    "artists": [{"id": "artist-2", "name": "Artist Two"}],
    "album": {"name": "Album Two", "id": "MPREb_album2"},
    "duration_seconds": 245,
    "length": "4:11",
    "thumbnail": [{"url": "https://example.com/song-2.jpg"}],
}

_WATCH_ROW = {
    "tracks": [_UPNEXT_CURRENT, _UPNEXT_SONG_TWO],
    "playlistId": "RDAMVMcurrent-track",
    "lyrics": "MPLYt_lyrics_browse_id",
    "related": "MPTRt_related_browse_id",
}

# --- /related fixtures -------------------------------------------------------

_RELATED_SONG_ATV_1 = {
    "title": "Related Song One",
    "videoId": "related-1",
    "videoType": "MUSIC_VIDEO_TYPE_ATV",
    "artists": [{"id": "artist-3", "name": "Artist Three"}],
    "album": {"name": "Album Three", "id": "MPREb_album3"},
    "duration_seconds": 180,
    "thumbnails": [{"url": "https://example.com/related-1.jpg"}],
}

_RELATED_SONG_ATV_2 = {
    "title": "Related Song Two",
    "videoId": "related-2",
    "videoType": "MUSIC_VIDEO_TYPE_ATV",
    "thumbnails": [{"url": "https://example.com/related-2.jpg"}],
}

_RELATED_SONG_UGC = {
    "title": "Fan Cover",
    "videoId": "related-ugc",
    "videoType": "MUSIC_VIDEO_TYPE_UGC",
    "thumbnails": [{"url": "https://example.com/ugc.jpg"}],
}

_RELATED_ALBUM_ONE = {
    "title": "Related Album",
    "browseId": "MPREb_relalbum",
    "artists": [{"id": "artist-4", "name": "Artist Four"}],
    "year": "2021",
    "audioPlaylistId": "OLAK5uy_relalbum",
    "thumbnails": [{"url": "https://example.com/relalbum.jpg"}],
}

_RELATED_ARTIST_ONE = {
    "title": "Related Artist",
    "browseId": "UC-relartist",
    "subscribers": "500K",
    "thumbnails": [{"url": "https://example.com/relartist.jpg"}],
}

_RELATED_PLAYLIST_ONE = {
    "title": "Recommended Playlist",
    "playlistId": "PL-recommended",
    "thumbnails": [{"url": "https://example.com/playlist.jpg"}],
    "description": "Playlist - YouTube Music",
}

_RELATED_ABOUT_THE_ARTIST_TEXT = (
    "Oasis were a rock band consisting of Liam Gallagher, Paul ..."
)

_RELATED_SECTIONS = [
    {
        "title": "You might also like",
        "contents": [_RELATED_SONG_ATV_1, _RELATED_SONG_UGC, None],
    },
    {"title": "More by the artist", "contents": [_RELATED_SONG_ATV_2]},
    {"title": "Albums", "contents": [_RELATED_ALBUM_ONE]},
    {"title": "Similar artists", "contents": [_RELATED_ARTIST_ONE]},
    {"title": "Recommended playlists", "contents": [_RELATED_PLAYLIST_ONE]},
    {"title": "About the artist", "contents": _RELATED_ABOUT_THE_ARTIST_TEXT},
]

_MAPPED_RELATED_SONG_ONE = {
    "track_id": "related-1",
    "title": "Related Song One",
    "artists": [{"id": "artist-3", "name": "Artist Three"}],
    "album": "Album Three",
    "album_id": "MPREb_album3",
    "duration_seconds": 180,
    "thumbnail_url": "https://example.com/related-1.jpg",
}

_MAPPED_RELATED_SONG_TWO = {
    "track_id": "related-2",
    "title": "Related Song Two",
    "artists": [],
    "album": None,
    "album_id": None,
    "duration_seconds": None,
    "thumbnail_url": "https://example.com/related-2.jpg",
}

_MAPPED_RELATED_ALBUM_ONE = {
    "id": "MPREb_relalbum",
    "title": "Related Album",
    "artists": [{"id": "artist-4", "name": "Artist Four"}],
    "year": "2021",
    "audio_playlist_id": "OLAK5uy_relalbum",
    "thumbnail_url": "https://example.com/relalbum.jpg",
}

_MAPPED_RELATED_ARTIST_ONE = {
    "id": "UC-relartist",
    "name": "Related Artist",
    "thumbnail_url": "https://example.com/relartist.jpg",
}

# --- /credits fixtures --------------------------------------------------------

_CREDITS_ROW = {
    "performed_by": {
        "localized_title": "Performed by",
        "data": ["Artist A", "Artist B"],
    },
    "written_by": {"localized_title": "Written by", "data": ["Writer A"]},
    "produced_by": {"localized_title": "Produced by", "data": ["Producer A"]},
    "music_metadata_provided_by": {
        "localized_title": "Music metadata provided by",
        "data": ["Metadata Co"],
    },
    "other_sections": [{"localized_title": "Piano", "data": ["Pianist A"]}],
}

_MAPPED_CREDITS = {
    "performed_by": {
        "localized_title": "Performed by",
        "names": ["Artist A", "Artist B"],
    },
    "written_by": {"localized_title": "Written by", "names": ["Writer A"]},
    "produced_by": {"localized_title": "Produced by", "names": ["Producer A"]},
    "music_metadata_provided_by": {
        "localized_title": "Music metadata provided by",
        "names": ["Metadata Co"],
    },
    "other_sections": [{"localized_title": "Piano", "names": ["Pianist A"]}],
}

_EMPTY_CREDITS = {
    "performed_by": None,
    "written_by": None,
    "produced_by": None,
    "music_metadata_provided_by": None,
    "other_sections": [],
}


@dataclass
class _FakeLyricLine:
    # Local, not imported from the library: the suite never names
    # ytmusicapi, and vars() works the same on any plain object.
    text: str
    start_time: int
    end_time: int
    id: int


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_search_provider, None)
    app.dependency_overrides.pop(get_current_user_id, None)


def _use_auth(user_id=_USER_ID):
    app.dependency_overrides[get_current_user_id] = lambda: user_id


def _use_provider(provider):
    app.dependency_overrides[get_search_provider] = lambda: provider


def _fake_provider(
    watch=_WATCH_ROW,
    watch_error=None,
    lyrics=None,
    lyrics_error=None,
    related=None,
    related_error=None,
    song=None,
    song_error=None,
    credits=None,
    credits_error=None,
):
    provider = MagicMock()
    if watch_error is not None:
        provider.get_watch_playlist.side_effect = watch_error
    else:
        provider.get_watch_playlist.return_value = watch
    if lyrics_error is not None:
        provider.get_lyrics.side_effect = lyrics_error
    else:
        provider.get_lyrics.return_value = lyrics
    if related_error is not None:
        provider.get_song_related.side_effect = related_error
    else:
        provider.get_song_related.return_value = related
    if song_error is not None:
        provider.get_song.side_effect = song_error
    else:
        provider.get_song.return_value = song
    if credits_error is not None:
        provider.get_song_credits.side_effect = credits_error
    else:
        provider.get_song_credits.return_value = credits
    return provider


# =============================================================================
# /upnext
# =============================================================================


def test_get_upnext_returns_tracks_mapped_field_by_field():
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {
        "tracks": [
            {
                "track_id": "current-track",
                "title": "Current Track",
                "artists": [],
                "album": None,
                "album_id": None,
                "duration_seconds": 187,
                "thumbnail_url": "https://example.com/current-large.jpg",
            },
            {
                "track_id": "song-2",
                "title": "Song Two",
                "artists": [{"id": "artist-2", "name": "Artist Two"}],
                "album": "Album Two",
                "album_id": "MPREb_album2",
                "duration_seconds": 245,
                "thumbnail_url": "https://example.com/song-2.jpg",
            },
        ]
    }


def test_get_upnext_explicit_duration_seconds_wins_over_length():
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 200
    assert response.json()["data"]["tracks"][1]["duration_seconds"] == 245


def test_get_upnext_unparseable_length_returns_null_duration():
    item = {**_UPNEXT_CURRENT, "length": "3m07"}
    row = {**_WATCH_ROW, "tracks": [item]}
    provider = _fake_provider(watch=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 200
    assert response.json()["data"]["tracks"][0]["duration_seconds"] is None


def test_get_upnext_first_item_without_artists_or_album_is_kept():
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 200
    first = response.json()["data"]["tracks"][0]
    assert first["artists"] == []
    assert first["album"] is None
    assert first["album_id"] is None


def test_get_upnext_single_call_and_lazy_probe_not_called():
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 200
    provider.get_watch_playlist.assert_called_once_with(videoId=_TRACK_ID, limit=50)
    provider.get_song.assert_not_called()


def test_get_upnext_empty_tracks_returns_empty_list():
    row = {**_WATCH_ROW, "tracks": []}
    provider = _fake_provider(watch=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 200
    assert response.json()["data"]["tracks"] == []


def test_get_upnext_degraded_item_returns_nulls_and_empty_artists():
    degraded = {
        "videoId": "degraded",
        "title": "Degraded Track",
        "thumbnail": None,
    }
    row = {**_WATCH_ROW, "tracks": [degraded]}
    provider = _fake_provider(watch=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 200
    track = response.json()["data"]["tracks"][0]
    assert track["artists"] == []
    assert track["album"] is None
    assert track["album_id"] is None
    assert track["duration_seconds"] is None
    assert track["thumbnail_url"] is None


@pytest.mark.parametrize("missing_key", ["videoId", "title"])
def test_get_upnext_item_missing_required_key_returns_upstream_error(missing_key):
    item = {k: v for k, v in _UPNEXT_CURRENT.items() if k != missing_key}
    row = {**_WATCH_ROW, "tracks": [item]}
    provider = _fake_provider(watch=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# =============================================================================
# /lyrics
# =============================================================================


def test_get_lyrics_happy_path_with_timestamps():
    lines = [
        _FakeLyricLine(text="Line one", start_time=9200, end_time=10630, id=1),
        _FakeLyricLine(text="Line two", start_time=10680, end_time=12540, id=2),
    ]
    raw = {"lyrics": lines, "source": "Source: LyricFind", "hasTimestamps": True}
    provider = _fake_provider(lyrics=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "lyrics": {
            "has_timestamps": True,
            "source": "Source: LyricFind",
            "lines": [
                {"text": "Line one", "start_ms": 9200, "end_ms": 10630},
                {"text": "Line two", "start_ms": 10680, "end_ms": 12540},
            ],
        }
    }


def test_get_lyrics_happy_path_without_timestamps_strips_carriage_returns():
    text = (
        "Today is gonna be the day\r\n"
        "That they're gonna throw it back to you\r\n"
        "\r\n"
        "Second verse\r\n"
    )
    raw = {"lyrics": text, "source": "Source: LyricFind", "hasTimestamps": False}
    provider = _fake_provider(lyrics=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 200
    lyrics = response.json()["data"]["lyrics"]
    assert lyrics["has_timestamps"] is False
    assert lyrics["lines"] == [
        {"text": "Today is gonna be the day", "start_ms": None, "end_ms": None},
        {
            "text": "That they're gonna throw it back to you",
            "start_ms": None,
            "end_ms": None,
        },
        {"text": "", "start_ms": None, "end_ms": None},
        {"text": "Second verse", "start_ms": None, "end_ms": None},
    ]
    assert all(
        line["start_ms"] is None and line["end_ms"] is None for line in lyrics["lines"]
    )


def test_get_lyrics_blank_line_between_stanzas_is_kept():
    raw = {"lyrics": "First\r\n\r\nSecond\r\n", "source": None, "hasTimestamps": False}
    provider = _fake_provider(lyrics=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 200
    lines = response.json()["data"]["lyrics"]["lines"]
    assert lines[1]["text"] == ""


def test_get_lyrics_no_lyrics_tab_returns_null_without_calling_provider():
    row = {**_WATCH_ROW, "lyrics": None}
    provider = _fake_provider(watch=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 200
    assert response.json()["data"] == {"lyrics": None}
    provider.get_lyrics.assert_not_called()


def test_get_lyrics_provider_returns_none_returns_null():
    provider = _fake_provider(lyrics=None)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 200
    assert response.json()["data"] == {"lyrics": None}


def test_get_lyrics_null_source_returns_null():
    raw = {"lyrics": "Some text", "source": None, "hasTimestamps": False}
    provider = _fake_provider(lyrics=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 200
    assert response.json()["data"]["lyrics"]["source"] is None


def test_get_lyrics_calls_provider_with_limit_one_and_browse_id():
    raw = {"lyrics": "text", "source": None, "hasTimestamps": False}
    provider = _fake_provider(lyrics=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 200
    provider.get_watch_playlist.assert_called_once_with(videoId=_TRACK_ID, limit=1)
    provider.get_lyrics.assert_called_once_with(_WATCH_ROW["lyrics"], timestamps=True)


def test_get_lyrics_dict_lines_instead_of_dataclass_returns_upstream_error():
    raw = {
        "lyrics": [{"text": "x", "start_time": 0, "end_time": 1, "id": 1}],
        "source": None,
        "hasTimestamps": True,
    }
    provider = _fake_provider(lyrics=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_lyrics_missing_has_timestamps_key_returns_upstream_error():
    raw = {"lyrics": "text", "source": None}
    provider = _fake_provider(lyrics=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


# =============================================================================
# /related
# =============================================================================


def test_get_related_happy_path_concatenates_song_sections():
    provider = _fake_provider(related=_RELATED_SECTIONS)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "songs": [_MAPPED_RELATED_SONG_ONE, _MAPPED_RELATED_SONG_TWO],
        "artists": [_MAPPED_RELATED_ARTIST_ONE],
        "albums": [_MAPPED_RELATED_ALBUM_ONE],
    }


@pytest.mark.parametrize(
    "video_type",
    ["MUSIC_VIDEO_TYPE_UGC", "MUSIC_VIDEO_TYPE_OMV", "OFFICIAL_SOURCE_MUSIC"],
)
def test_get_related_non_atv_item_never_appears(video_type):
    item = {**_RELATED_SONG_ATV_1, "videoId": "non-atv", "videoType": video_type}
    sections = [{"title": "You might also like", "contents": [item]}]
    provider = _fake_provider(related=sections)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    assert response.json()["data"]["songs"] == []


def test_get_related_song_item_without_video_type_key_is_excluded():
    item = {k: v for k, v in _RELATED_SONG_ATV_1.items() if k != "videoType"}
    sections = [{"title": "You might also like", "contents": [item]}]
    provider = _fake_provider(related=sections)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    assert response.json()["data"]["songs"] == []


def test_get_related_string_section_and_none_item_are_skipped():
    provider = _fake_provider(related=_RELATED_SECTIONS)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    songs = response.json()["data"]["songs"]
    assert len(songs) == 2


def test_get_related_sections_identified_by_shape_not_title():
    localized_sections = [
        {**section, "title": "タイトル不明"} for section in _RELATED_SECTIONS
    ]
    provider = _fake_provider(related=localized_sections)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "songs": [_MAPPED_RELATED_SONG_ONE, _MAPPED_RELATED_SONG_TWO],
        "artists": [_MAPPED_RELATED_ARTIST_ONE],
        "albums": [_MAPPED_RELATED_ALBUM_ONE],
    }


def test_get_related_album_and_artist_classified_by_key_presence_not_truth():
    album = {**_RELATED_ALBUM_ONE, "audioPlaylistId": None}
    artist = {**_RELATED_ARTIST_ONE, "subscribers": None}
    sections = [
        {"title": "Albums", "contents": [album]},
        {"title": "Similar artists", "contents": [artist]},
    ]
    provider = _fake_provider(related=sections)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["albums"] == [{**_MAPPED_RELATED_ALBUM_ONE, "audio_playlist_id": None}]
    assert data["artists"] == [_MAPPED_RELATED_ARTIST_ONE]


def test_get_related_no_related_tab_returns_empty_lists_without_calling_provider():
    row = {**_WATCH_ROW, "related": None}
    provider = _fake_provider(watch=row)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    assert response.json()["data"] == {"songs": [], "artists": [], "albums": []}
    provider.get_song_related.assert_not_called()


def test_get_related_provider_returns_empty_list_returns_empty_lists():
    provider = _fake_provider(related=[])
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    assert response.json()["data"] == {"songs": [], "artists": [], "albums": []}


def test_get_related_no_surviving_atv_item_returns_empty_songs():
    sections = [
        {
            "title": "You might also like",
            "contents": [{**_RELATED_SONG_ATV_1, "videoType": "MUSIC_VIDEO_TYPE_UGC"}],
        },
        {"title": "Albums", "contents": [_RELATED_ALBUM_ONE]},
        {"title": "Similar artists", "contents": [_RELATED_ARTIST_ONE]},
    ]
    provider = _fake_provider(related=sections)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["songs"] == []
    assert data["albums"] == [_MAPPED_RELATED_ALBUM_ONE]
    assert data["artists"] == [_MAPPED_RELATED_ARTIST_ONE]


def test_get_related_calls_provider_with_limit_one_and_browse_id():
    provider = _fake_provider(related=[])
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == 200
    provider.get_watch_playlist.assert_called_once_with(videoId=_TRACK_ID, limit=1)
    provider.get_song_related.assert_called_once_with(_WATCH_ROW["related"])


# =============================================================================
# /credits
# =============================================================================


def test_get_credits_happy_path_maps_all_five_keys():
    provider = _fake_provider(credits=_CREDITS_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    assert response.json()["data"] == _MAPPED_CREDITS


def test_get_credits_single_call_no_watch_playlist_no_album_no_song():
    provider = _fake_provider(credits=_CREDITS_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    provider.get_song_credits.assert_called_once_with("MPTC" + _TRACK_ID)
    provider.get_album.assert_not_called()
    provider.get_watch_playlist.assert_not_called()
    provider.get_song.assert_not_called()


def test_get_credits_only_other_sections_returns_four_nulls():
    raw = {"other_sections": [{"localized_title": "Piano", "data": ["Pianist A"]}]}
    provider = _fake_provider(credits=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["performed_by"] is None
    assert data["written_by"] is None
    assert data["produced_by"] is None
    assert data["music_metadata_provided_by"] is None
    assert data["other_sections"] == [
        {"localized_title": "Piano", "names": ["Pianist A"]}
    ]


def test_get_credits_typed_section_with_empty_data_returns_empty_names_not_null():
    raw = {
        **_CREDITS_ROW,
        "performed_by": {"localized_title": "Performed by", "data": []},
    }
    provider = _fake_provider(credits=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    assert response.json()["data"]["performed_by"] == {
        "localized_title": "Performed by",
        "names": [],
    }


def test_get_credits_unknown_section_falls_into_other_sections():
    raw = {
        "other_sections": [
            {"localized_title": "Piano", "data": ["Pianist A"]},
            {"localized_title": "Mixed by", "data": ["Mixer A"]},
        ]
    }
    provider = _fake_provider(credits=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    assert response.json()["data"]["other_sections"] == [
        {"localized_title": "Piano", "names": ["Pianist A"]},
        {"localized_title": "Mixed by", "names": ["Mixer A"]},
    ]


def test_get_credits_classification_does_not_depend_on_localized_title():
    localized = {
        key: (
            {**section, "localized_title": "タイトル不明"}
            if key != "other_sections"
            else [{**s, "localized_title": "タイトル不明"} for s in section]
        )
        for key, section in _CREDITS_ROW.items()
    }
    provider = _fake_provider(credits=localized)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["performed_by"]["names"] == _MAPPED_CREDITS["performed_by"]["names"]
    assert data["written_by"]["names"] == _MAPPED_CREDITS["written_by"]["names"]
    assert data["produced_by"]["names"] == _MAPPED_CREDITS["produced_by"]["names"]
    assert (
        data["music_metadata_provided_by"]["names"]
        == _MAPPED_CREDITS["music_metadata_provided_by"]["names"]
    )
    assert (
        data["other_sections"][0]["names"]
        == _MAPPED_CREDITS["other_sections"][0]["names"]
    )


def test_get_credits_response_is_not_paginated():
    provider = _fake_provider(credits=_CREDITS_ROW)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert "page" not in response.json()["data"]


def test_get_credits_navigation_key_error_with_track_confirmed_returns_empty_200():
    provider = _fake_provider(
        credits_error=KeyError("sections"),
        song={"playabilityStatus": {"status": "OK"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    assert response.json()["data"] == _EMPTY_CREDITS
    provider.get_song.assert_called_once_with(_TRACK_ID)


def test_get_credits_navigation_index_error_with_track_confirmed_returns_empty_200():
    provider = _fake_provider(
        credits_error=IndexError("list index out of range"),
        song={"playabilityStatus": {"status": "OK"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    assert response.json()["data"] == _EMPTY_CREDITS


def test_get_credits_navigation_key_error_with_unplayable_track_returns_empty_200():
    provider = _fake_provider(
        credits_error=KeyError("sections"),
        song={"playabilityStatus": {"status": "UNPLAYABLE"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 200
    assert response.status_code != 404
    assert response.json()["data"] == _EMPTY_CREDITS


def test_get_credits_navigation_key_error_with_track_missing_returns_404():
    provider = _fake_provider(
        credits_error=KeyError("a distinctive credits navigation message"),
        song={"playabilityStatus": {"status": "ERROR"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "track_not_found"}
    provider.get_song.assert_called_once_with(_TRACK_ID)


def test_get_credits_server_error_with_track_missing_returns_404():
    provider = _fake_provider(
        credits_error=PROVIDER_ERRORS[0]("a distinctive credits server error"),
        song={"playabilityStatus": {"status": "ERROR"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "track_not_found"}


def test_get_credits_404_body_never_leaks_the_original_message():
    provider = _fake_provider(
        credits_error=KeyError("a message that must never leak from credits"),
        song={"playabilityStatus": {"status": "ERROR"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 404
    body = response.json()
    assert set(body.keys()) == {"ok", "reason"}
    assert "a message that must never leak from credits" not in response.text


def test_get_credits_server_error_with_track_playable_returns_upstream_error():
    provider = _fake_provider(
        credits_error=PROVIDER_ERRORS[0]("provider backend failed"),
        song={"playabilityStatus": {"status": "OK"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_credits_server_error_with_track_unplayable_returns_upstream_error():
    provider = _fake_provider(
        credits_error=PROVIDER_ERRORS[0]("provider backend failed"),
        song={"playabilityStatus": {"status": "UNPLAYABLE"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_credits_connection_error_never_calls_the_probe():
    provider = _fake_provider(credits_error=requests.exceptions.ConnectionError())
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    provider.get_song.assert_not_called()


def test_get_credits_timeout_never_calls_the_probe():
    provider = _fake_provider(credits_error=requests.exceptions.Timeout())
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    provider.get_song.assert_not_called()


def test_get_credits_value_error_from_library_returns_upstream_error():
    provider = _fake_provider(credits_error=ValueError("library misuse"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_credits_type_error_returns_upstream_error():
    provider = _fake_provider(credits_error=TypeError("layout changed"))
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize(
    "song_error,status,reason",
    [
        (PROVIDER_ERRORS[0]("probe also failed"), 502, "upstream_error"),
        (requests.exceptions.Timeout(), 504, "upstream_timeout"),
    ],
)
def test_get_credits_probe_itself_failing_returns_expected_status(
    song_error, status, reason
):
    provider = _fake_provider(
        credits_error=KeyError("sections"),
        song_error=song_error,
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == status
    assert response.json() == {"ok": False, "reason": reason}


def test_get_credits_probe_missing_playability_status_returns_upstream_error():
    provider = _fake_provider(
        credits_error=KeyError("sections"),
        song={"some_other_field": True},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_credits_section_missing_localized_title_returns_upstream_error():
    raw = {
        "other_sections": [{"data": ["Pianist A"]}],
    }
    provider = _fake_provider(credits=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_credits_missing_other_sections_returns_upstream_error():
    raw = {"performed_by": {"localized_title": "Performed by", "data": ["Artist A"]}}
    provider = _fake_provider(credits=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_credits_names_as_string_returns_upstream_error():
    raw = {
        "performed_by": {"localized_title": "Performed by", "data": "not-a-list"},
        "other_sections": [],
    }
    provider = _fake_provider(credits=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_credits_unauthenticated_returns_unauthorized():
    provider = _fake_provider(credits=_CREDITS_ROW)
    _use_provider(provider)

    response = client.get(f"/tracks/{_TRACK_ID}/credits")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    provider.get_song_credits.assert_not_called()


# =============================================================================
# 404 track_not_found and the probe (parametrized over the three endpoints)
# =============================================================================

_ENDPOINTS = ["upnext", "lyrics", "related"]


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_missing_track_status_error_returns_track_not_found(endpoint):
    provider = _fake_provider(
        watch_error=PROVIDER_ERRORS[0]("No content returned by the server."),
        song={"playabilityStatus": {"status": "ERROR"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/{endpoint}")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "track_not_found"}
    provider.get_song.assert_called_once_with(_TRACK_ID)


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_unplayable_status_returns_upstream_error_not_not_found(endpoint):
    provider = _fake_provider(
        watch_error=PROVIDER_ERRORS[0]("Sign in to confirm your age."),
        song={"playabilityStatus": {"status": "UNPLAYABLE"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/{endpoint}")

    assert response.status_code == 502
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_ok_status_returns_upstream_error(endpoint):
    provider = _fake_provider(
        watch_error=PROVIDER_ERRORS[0]("Unexpected server response."),
        song={"playabilityStatus": {"status": "OK"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/{endpoint}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_probe_missing_playability_status_returns_upstream_error(endpoint):
    provider = _fake_provider(
        watch_error=PROVIDER_ERRORS[0]("Backend request failed."),
        song={"some_other_field": True},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/{endpoint}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_probe_itself_failing_with_server_error_returns_upstream_error(endpoint):
    provider = _fake_provider(
        watch_error=PROVIDER_ERRORS[0]("Original watch playlist failure."),
        song_error=PROVIDER_ERRORS[0]("Probe also failed."),
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/{endpoint}")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_probe_itself_timing_out_returns_upstream_timeout(endpoint):
    provider = _fake_provider(
        watch_error=PROVIDER_ERRORS[0]("Original watch playlist failure."),
        song_error=requests.exceptions.Timeout(),
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/{endpoint}")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_track_not_found_body_never_leaks_the_original_message():
    provider = _fake_provider(
        watch_error=PROVIDER_ERRORS[0]("a message that must never leak"),
        song={"playabilityStatus": {"status": "ERROR"}},
    )
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert response.status_code == 404
    body = response.json()
    assert set(body.keys()) == {"ok", "reason"}
    assert "a message that must never leak" not in response.text


# =============================================================================
# Upstream failures (parametrized over the three endpoints)
# =============================================================================


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
@pytest.mark.parametrize(
    "error,status,reason",
    [
        (requests.exceptions.ConnectionError(), 502, "upstream_error"),
        (requests.exceptions.Timeout(), 504, "upstream_timeout"),
        (PROVIDER_ERRORS[1]("unparseable"), 502, "upstream_error"),
        (KeyError("layout changed"), 502, "upstream_error"),
        (TypeError("layout changed"), 502, "upstream_error"),
        (ValueError("library misuse"), 502, "upstream_error"),
        (IndexError("library misuse"), 502, "upstream_error"),
    ],
)
def test_watch_playlist_failure_maps_to_expected_status(
    endpoint, error, status, reason
):
    provider = _fake_provider(watch_error=error)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/{endpoint}")

    assert response.status_code == status
    assert response.json() == {"ok": False, "reason": reason}


@pytest.mark.parametrize(
    "error,status,reason",
    [
        (requests.exceptions.ConnectionError(), 502, "upstream_error"),
        (requests.exceptions.Timeout(), 504, "upstream_timeout"),
        (PROVIDER_ERRORS[0]("provider backend failed"), 502, "upstream_error"),
        (PROVIDER_ERRORS[1]("unparseable"), 502, "upstream_error"),
        (KeyError("layout changed"), 502, "upstream_error"),
        (TypeError("layout changed"), 502, "upstream_error"),
        (ValueError("library misuse"), 502, "upstream_error"),
        (IndexError("library misuse"), 502, "upstream_error"),
    ],
)
def test_lyrics_call_failure_after_track_exists_is_never_not_found(
    error, status, reason
):
    provider = _fake_provider(lyrics_error=error)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert response.status_code == status
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": reason}


@pytest.mark.parametrize(
    "error,status,reason",
    [
        (requests.exceptions.ConnectionError(), 502, "upstream_error"),
        (requests.exceptions.Timeout(), 504, "upstream_timeout"),
        (PROVIDER_ERRORS[0]("provider backend failed"), 502, "upstream_error"),
        (PROVIDER_ERRORS[1]("unparseable"), 502, "upstream_error"),
        (KeyError("layout changed"), 502, "upstream_error"),
        (TypeError("layout changed"), 502, "upstream_error"),
        (ValueError("library misuse"), 502, "upstream_error"),
        (IndexError("library misuse"), 502, "upstream_error"),
    ],
)
def test_related_call_failure_after_track_exists_is_never_not_found(
    error, status, reason
):
    provider = _fake_provider(related_error=error)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert response.status_code == status
    assert response.status_code != 404
    assert response.json() == {"ok": False, "reason": reason}


# =============================================================================
# Transversal
# =============================================================================


@pytest.mark.parametrize("endpoint", _ENDPOINTS)
def test_unauthenticated_returns_unauthorized(endpoint):
    provider = _fake_provider()
    _use_provider(provider)

    response = client.get(f"/tracks/{_TRACK_ID}/{endpoint}")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    provider.get_watch_playlist.assert_not_called()


@pytest.mark.parametrize(
    "path", ["/tracks/upnext", "/tracks//upnext", "/tracks//credits"]
)
def test_no_route_matches_returns_not_found(path):
    _use_auth()

    response = client.get(path)

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "not_found"}


def test_upnext_response_is_not_paginated():
    provider = _fake_provider()
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/upnext")

    assert "page" not in response.json()["data"]


def test_lyrics_response_is_not_paginated():
    raw = {"lyrics": "text", "source": None, "hasTimestamps": False}
    provider = _fake_provider(lyrics=raw)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/lyrics")

    assert "page" not in response.json()["data"]


def test_related_response_is_not_paginated():
    provider = _fake_provider(related=_RELATED_SECTIONS)
    _use_provider(provider)
    _use_auth()

    response = client.get(f"/tracks/{_TRACK_ID}/related")

    assert "page" not in response.json()["data"]
