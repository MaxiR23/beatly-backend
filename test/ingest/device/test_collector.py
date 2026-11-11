# test/ingest/device/test_collector.py
#
# Tests for the collector that runs on the phone (ingest/device/collector.py).
# No network and no provider: a fake provider, a fake HTTP session and a guard
# that fails any real requests call.
#
# Tested:
# - normalize_title: one case per rule (lowercase, accents, official/video/
#   lyric/visualizer/audio/live/en vivo groups and suffixes, ft/feat) and the
#   control "Live Forever"
# - Matching: the search is songs, limit 5, with the title and at most two
#   artists; the first result with the same title (or contained in the video's)
#   and a shared artist id is "oficial"; the title without a shared artist is
#   "solo por nombre"; nothing is "sin match"; an audio video is used as is,
#   without a search; an unavailable item or one missing a field is "datos
#   incompletos"; an override wins over search, audio and cache and its audio
#   must be the one asked for; the duration difference is audio minus video and
#   never decides; a repeated audio is "repetida"
# - Six searches at most in flight; a transient provider error is retried (1 s
#   and 2 s) and an exhausted one is "error del proveedor" and is not cached;
#   the cache holds only "oficial", is written atomically, a second run with it
#   makes no search, and an unreadable one exits 1
# - The input files: the versioned ones parse, rejections, a 404 for the
#   overrides is "no exceptions", a malformed one exits 1
# - The submission: the shape the back expects (and the back's own model
#   accepts it), the title of the entry or of the source, no repeated track
#   across playlists, only the saved statuses go and the skipped are counted
# - The back's limits and patterns, checked before sending: 300 tracks per
#   playlist, 50 playlists per genre, 50 genres, 10 000 distinct tracks, the
#   id and slug patterns; an input over one exits 1 naming genre, playlist and
#   limit, on a dry run too, and sends nothing; the collector's constants and
#   patterns equal the ones in models/ingest.py (read, never imported by it)
# - The CSV (name, header, one row per item, every column formula-safe, ids
#   included) and the summary
#   (one line per playlist and a total with the eight counters, on out and not
#   in the log)
# - Exit codes: a dry run sends nothing and exits 0; --write posts once and
#   exits 0; a provider error, an unread playlist or a playlist with no saved
#   song exits 1 without sending but with CSV and summary; no genres, bad
#   arguments (2) and bad config exit as they should; stdin is never read
# - BEATLY_API_URL: https accepted; http only to localhost, 127.0.0.1 and the
#   private ranges (172.15 and 172.32 rejected); a public host over http exits 1
# - The send: retries network errors and 502/503/504 with waits of 5, 15 and
#   45 s, gives up after four attempts, never retries a 4xx, rejects other
#   counts, and never logs the token
# - Isolation: the file imports only the standard library, requests and the
#   provider library, parses as Python 3.11, and its device requirements are
#   the back's pinned lines
#
# What is covered:
# - Every function of the collector with the network replaced. The real
#   provider, the phone and cron are checked by hand (docs/ingest.md)
#
# Run with: pytest test/ingest/device/test_collector.py -v
#
# SEE: ingest/device/collector.py, docs/ingest.md, routes/ingest.py

import ast
import csv
import json
import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from ingest.device import collector
from models.ingest import GenreIngestRequest

ROOT = Path(__file__).resolve().parents[3]
_TOKEN = "fake-collector-token-made-in-the-test-xyz"
_API = "https://api.example.com"
_SOURCES_URL = f"{collector.RAW_BASE_URL}/genre_sources.json"
_OVERRIDES_URL = f"{collector.RAW_BASE_URL}/overrides.json"
_NOW = datetime(2026, 11, 10, 12, 30, 45, tzinfo=UTC)
_COVER = "https://example.com/cover.jpg"


@pytest.fixture(autouse=True)
def _no_real_requests(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise AssertionError("a test made a real HTTP request")

    monkeypatch.setattr(requests.Session, "request", refuse)


# --- Fakes -------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, payload=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload
        self._invalid = invalid_json

    def json(self):
        if self._invalid:
            raise requests.exceptions.JSONDecodeError("bad", "doc", 0)
        return self._payload


class FakeSession:
    # get: routes maps a url to a response or a list of them (consumed in
    # order, the last one repeats); an exception in the list is raised. post:
    # post_script is consumed the same way; empty means "accept with the
    # counts of the payload".
    def __init__(self):
        self.routes: dict[str, object] = {}
        self.post_script: list[object] = []
        self.gets: list[str] = []
        self.posts: list[dict] = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        script = self.routes[url]
        if isinstance(script, list):
            step = script.pop(0) if len(script) > 1 else script[0]
        else:
            step = script
        if isinstance(step, Exception):
            raise step
        return step

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        if self.post_script:
            step = (
                self.post_script.pop(0)
                if len(self.post_script) > 1
                else self.post_script[0]
            )
        else:
            counts = collector.genre_counts(json)
            step = FakeResponse(200, {"ok": True, "data": counts})
        if isinstance(step, Exception):
            raise step
        return step


class FakeProvider:
    def __init__(self):
        self.pages: dict[str, object] = {}
        self.watch: dict[str, object] = {}
        self.search_answers: dict[str, object] = {}
        self.search_calls: list[tuple] = []
        self.get_playlist_calls: list[tuple] = []
        self.watch_calls: list[tuple] = []
        self.search_errors: list[Exception] = []
        self.search_delay = 0.0
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    def get_playlist(self, playlist_id, limit=100):
        self.get_playlist_calls.append((playlist_id, limit))
        page = self.pages[playlist_id]
        if isinstance(page, Exception):
            raise page
        return page

    def search(self, query, filter=None, limit=20):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.search_calls.append((query, filter, limit))
            error = self.search_errors.pop(0) if self.search_errors else None
        try:
            if self.search_delay:
                time.sleep(self.search_delay)
            if error is not None:
                raise error
            for title, answer in self.search_answers.items():
                if query.startswith(title):
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
            return []
        finally:
            with self._lock:
                self._in_flight -= 1

    def get_watch_playlist(self, videoId=None, limit=25):
        self.watch_calls.append((videoId, limit))
        answer = self.watch[videoId]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _artists(*pairs):
    return [{"id": i, "name": n} for i, n in pairs]


_ARTIST = _artists(("UCa", "Artist A"))


def _album(n=1):
    return {"name": f"Album {n}", "id": f"MPREb_{n}"}


def _video(n, **overrides):
    item = {
        "videoId": f"vid{n}",
        "title": f"Song {n} (Official Video)",
        "artists": _ARTIST,
        "album": None,
        "videoType": "MUSIC_VIDEO_TYPE_OMV",
        "isAvailable": True,
        "duration_seconds": 200,
        "thumbnails": [{"url": "https://example.com/small.jpg"}],
        **overrides,
    }
    return item


def _audio(n, **overrides):
    # A search result or a watch item of the audio of video n.
    item = {
        "videoId": f"aud{n}",
        "title": f"Song {n}",
        "artists": _ARTIST,
        "album": _album(n),
        "videoType": "MUSIC_VIDEO_TYPE_ATV",
        "duration_seconds": 203,
        "thumbnails": [{"url": "https://example.com/small.jpg"}, {"url": _COVER}],
        **overrides,
    }
    return item


def _resolver(provider=None, overrides=None, cache=None, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return collector.Resolver(
        provider or FakeProvider(),
        overrides=overrides or {},
        cache=cache if cache is not None else {},
        sleep=sleeps.append,
    )


def _provider_for(*videos, answers=None):
    provider = FakeProvider()
    provider.pages["PL1"] = {
        "title": "Source",
        "description": "Desc",
        "tracks": list(videos),
    }
    for video in videos:
        n = video["videoId"].removeprefix("vid")
        provider.search_answers[video["title"]] = (answers or {}).get(
            video["videoId"], [_audio(n)]
        )
    return provider


# --- Harness -----------------------------------------------------------------


class Harness:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.config_path = tmp_path / "collector.env"
        self.data_dir = tmp_path / "data"
        self.session = FakeSession()
        self.provider = FakeProvider()
        self.sleeps: list[float] = []
        self.write_config()
        self.set_sources([("rock", [{"source": "PL1"}])])
        self.session.routes[_OVERRIDES_URL] = FakeResponse(200, {})

    def write_config(self, text=None, mode=0o600):
        text = text or f"BEATLY_API_URL={_API}\nBEATLY_INGEST_TOKEN={_TOKEN}\n"
        self.config_path.write_text(text)
        os.chmod(self.config_path, mode)

    def set_sources(self, genres):
        data = {"genres": [{"slug": s, "playlists": p} for s, p in genres]}
        self.session.routes[_SOURCES_URL] = FakeResponse(200, data)

    def run(self, *argv, out=None):
        out = out if out is not None else SimpleNamespace(text="")
        stream = _Out(out)
        code = collector.main(
            list(argv),
            config_path=self.config_path,
            data_dir=self.data_dir,
            session=self.session,
            provider=self.provider,
            out=stream,
            now=_NOW,
            sleep=self.sleeps.append,
        )
        self.stdout = out.text
        return code

    def reports(self):
        return sorted((self.data_dir / "reports").glob("*.csv"))


class _Out:
    def __init__(self, holder):
        self.holder = holder

    def write(self, text):
        self.holder.text += text


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def _two_songs(h):
    h.provider = _provider_for(_video(1), _video(2))
    return h


# --- normalize_title ---------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Song (Official Video)", "song"),
        ("Cancion (Video Oficial)", "cancion"),
        ("Song [Lyric Video]", "song"),
        ("Song (Lyrics)", "song"),
        ("Song (Visualizer)", "song"),
        ("Song (Audio)", "song"),
        ("Song (Live at Wembley)", "song"),
        ("Cancion (En Vivo)", "cancion"),
        ("Song (feat. Other Artist)", "song"),
        ("Song [ft Other]", "song"),
        ("Song ft. Other Artist", "song"),
        ("Song Feat Other", "song"),
        ("Song - Official Video", "song"),
        ("Song - Live at Wembley", "song"),
        ("Song (Official Video) [feat. X]", "song"),
        ("Señorita Áé", "senorita ae"),
        ("Don't Stop!", "don t stop"),
        ("Song - Remastered 2011", "song remastered 2011"),
        ("Song (Remix)", "song remix"),
        ("Live Forever", "live forever"),
        ("Video Killed the Radio Star", "video killed the radio star"),
        ("(Official Video)", ""),
        ("", ""),
    ],
)
def test_normalize_title(title, expected):
    assert collector.normalize_title(title) == expected


# --- Matching ----------------------------------------------------------------


def test_search_is_songs_limit_5_with_title_and_two_artists():
    video = _video(
        1, artists=_artists(("UCa", "Artist A"), ("UCb", "Artist B"), ("UCc", "C"))
    )
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [_audio(1)]

    _resolver(provider).resolve(video)

    assert provider.search_calls == [
        ("Song 1 (Official Video) Artist A Artist B", "songs", 5)
    ]


def test_first_result_with_matching_title_and_shared_artist_is_official():
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [
        _audio(9, title="Another song"),
        _audio(1, videoId="firstmatch"),
        _audio(1, videoId="secondmatch"),
    ]

    result = _resolver(provider).resolve(_video(1))

    assert result.status is collector.Status.OFFICIAL
    assert result.audio_id == "firstmatch"
    assert result.track.title == "Song 1"
    assert result.track.album_id == "MPREb_1"
    assert result.track.thumbnail_url == _COVER


def test_result_title_contained_in_video_title_matches():
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [_audio(1, title="Song 1")]
    video = _video(1, title="Song 1 Extended Mix Version")

    result = _resolver(provider).resolve(video)

    assert result.status is collector.Status.OFFICIAL


def test_matching_title_without_shared_artist_is_name_only():
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [_audio(1, artists=_artists(("UCz", "Z")))]

    result = _resolver(provider).resolve(_video(1))

    assert result.status is collector.Status.NAME_ONLY
    assert result.track is None


def test_no_results_and_no_title_match_are_no_match():
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [_audio(7, title="Something else")]

    assert _resolver(provider).resolve(_video(1)).status is collector.Status.NO_MATCH
    provider.search_answers["Song 1"] = []
    assert _resolver(provider).resolve(_video(1)).status is collector.Status.NO_MATCH


def test_a_title_that_normalizes_to_nothing_never_matches():
    provider = FakeProvider()
    provider.search_answers["(Official Video)"] = [_audio(1, title="(Live)")]

    result = _resolver(provider).resolve(_video(1, title="(Official Video)"))

    assert result.status is collector.Status.NO_MATCH


def test_atv_item_is_used_as_is_without_search():
    provider = FakeProvider()
    item = _audio(1, videoId="vid1", album=_album(1))

    result = _resolver(provider).resolve(item)

    assert result.status is collector.Status.ALREADY_AUDIO
    assert result.audio_id == "vid1"
    assert provider.search_calls == []


def test_atv_item_without_album_is_incomplete():
    provider = FakeProvider()

    result = _resolver(provider).resolve(_audio(1, album=None))

    assert result.status is collector.Status.INCOMPLETE
    assert provider.search_calls == []


def test_unavailable_item_is_incomplete_without_calls():
    provider = FakeProvider()

    result = _resolver(provider).resolve(_video(1, isAvailable=False))
    missing = _resolver(provider).resolve(_video(1, videoId=None, isAvailable=True))

    assert result.status is missing.status is collector.Status.INCOMPLETE
    assert provider.search_calls == provider.watch_calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"album": None},
        {"album": {"name": "A", "id": None}},
        {"album": {"name": "", "id": "MPREb_1"}},
        {"duration_seconds": None},
        {"thumbnails": []},
        {"thumbnails": [{"url": "http://example.com/a.jpg"}]},
        {"duration_seconds": 86_401},
    ],
)
def test_accepted_result_missing_a_field_is_incomplete(mutation):
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [_audio(1, **mutation)]

    result = _resolver(provider).resolve(_video(1))

    assert result.status is collector.Status.INCOMPLETE
    assert result.track is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"artists": []},
        {"artists": _artists((None, "No id"))},
        {"artists": _artists(("", "No id"))},
        {"artists": _artists(("UCa", ""))},
        {"title": ""},
        {"title": "x" * 301},
        {"videoId": "has space"},
        {"album": {"name": "A", "id": "bad id"}},
    ],
)
def test_track_row_is_none_when_the_back_would_reject_the_row(mutation):
    assert collector.track_row(_audio(1, **mutation)) is None


def test_a_secondary_artist_without_id_is_kept_with_a_null_id():
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [
        _audio(1, artists=[{"id": "UCa", "name": "Artist A"}, {"name": "Guest"}])
    ]

    result = _resolver(provider).resolve(_video(1))

    assert result.track.artists == (("UCa", "Artist A"), (None, "Guest"))


def test_duration_from_a_length_text():
    item = _audio(1, duration_seconds=None, length="3:32")
    assert collector.duration_seconds(item) == 212
    item = _audio(1, duration_seconds=None, length="1:01:01")
    assert collector.duration_seconds(item) == 3661
    assert collector.duration_seconds(_audio(1, duration_seconds=None)) is None


def test_override_wins_over_search_atv_and_cache():
    provider = FakeProvider()
    provider.watch["audOV"] = {"tracks": [_audio(5, videoId="audOV")]}
    cache = {"vid1": collector.track_row(_audio(1))}
    atv = _audio(1, videoId="vid1")
    resolver = _resolver(provider, overrides={"vid1": "audOV"}, cache=cache)

    for item in (_video(1), atv):
        result = resolver.resolve(item)
        assert result.status is collector.Status.OVERRIDE
        assert result.audio_id == "audOV"
    assert provider.search_calls == []
    assert provider.watch_calls == [("audOV", 1), ("audOV", 1)]


def test_override_with_other_first_video_is_a_config_error():
    provider = FakeProvider()
    provider.watch["audOV"] = {"tracks": [_audio(5, videoId="somethingelse")]}

    with pytest.raises(collector.ConfigError):
        _resolver(provider, overrides={"vid1": "audOV"}).resolve(_video(1))


def test_override_with_incomplete_audio_is_incomplete():
    provider = FakeProvider()
    provider.watch["audOV"] = {"tracks": [_audio(5, videoId="audOV", album=None)]}

    result = _resolver(provider, overrides={"vid1": "audOV"}).resolve(_video(1))

    assert result.status is collector.Status.INCOMPLETE


def test_override_provider_failure_is_a_provider_error():
    provider = FakeProvider()
    provider.watch["audOV"] = requests.ConnectionError("down")
    sleeps = []

    result = _resolver(provider, overrides={"vid1": "audOV"}, sleeps=sleeps).resolve(
        _video(1)
    )

    assert result.status is collector.Status.PROVIDER_ERROR
    assert sleeps == [1.0, 2.0]


def test_duration_diff_is_audio_minus_video_and_never_decides():
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [_audio(1, duration_seconds=1000)]

    far = _resolver(provider).resolve(_video(1, duration_seconds=200))
    shorter = _resolver(provider).resolve(_video(1, duration_seconds=1300))

    assert far.status is shorter.status is collector.Status.OFFICIAL
    assert far.duration_diff == 800
    assert shorter.duration_diff == -300


def test_repeated_audio_in_a_playlist_is_marked_repeated():
    provider = _provider_for(
        _video(1), _video(2), answers={"vid2": [_audio(1, title="Song 2")]}
    )
    h_playlists = collector.read_playlists(
        provider,
        [
            collector.GenreSource(
                "rock", (collector.SourcePlaylist("PL1", None, None, None),)
            )
        ],
        sleep=lambda s: None,
    )

    collector.resolve_all(_resolver(provider), h_playlists)

    statuses = [r.status for r in h_playlists[0].resolutions]
    assert statuses == [collector.Status.OFFICIAL, collector.Status.REPEATED]


# --- Concurrency, retries and cache ------------------------------------------


def test_never_more_than_six_searches_in_flight():
    videos = [_video(i) for i in range(20)]
    provider = _provider_for(*videos)
    provider.search_delay = 0.02
    playlists = collector.read_playlists(
        provider,
        [
            collector.GenreSource(
                "rock", (collector.SourcePlaylist("PL1", None, None, None),)
            )
        ],
        sleep=lambda s: None,
    )

    collector.resolve_all(_resolver(provider), playlists)

    assert len(provider.search_calls) == 20
    assert 1 < provider.max_in_flight <= 6


def test_transient_provider_error_is_retried_then_resolves():
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [_audio(1)]
    provider.search_errors = [requests.ConnectionError("x"), ValueError("layout")]
    sleeps = []

    result = _resolver(provider, sleeps=sleeps).resolve(_video(1))

    assert result.status is collector.Status.OFFICIAL
    assert sleeps == [1.0, 2.0]
    assert len(provider.search_calls) == 3


def test_exhausted_retries_give_provider_error_and_nothing_is_cached(h):
    h.provider = _provider_for(_video(1))
    h.provider.search_errors = [requests.Timeout("t")] * 3

    code = h.run("genres", "--write")

    assert code == 1
    assert h.sleeps == [1.0, 2.0]
    assert "error del proveedor=1" in h.stdout
    assert h.session.posts == []
    assert json.loads((h.data_dir / "cache.json").read_text())["entries"] == {}


def test_second_run_with_full_cache_makes_no_search(h):
    h.provider = _provider_for(_video(1), _video(2))

    assert h.run("genres") == 0
    first_searches = len(h.provider.search_calls)
    h.provider.search_calls.clear()
    assert h.run("genres") == 0

    assert first_searches == 2
    assert h.provider.search_calls == []
    assert "oficial=2" in h.stdout


def test_only_official_results_are_cached(h):
    h.provider = _provider_for(
        _video(1),
        _video(2),
        _video(3),
        answers={"vid2": [_audio(2, artists=_artists(("UCz", "Z")))], "vid3": []},
    )
    h.provider.pages["PL1"]["tracks"].append(_audio(4, videoId="vid4"))

    code = h.run("genres")

    entries = json.loads((h.data_dir / "cache.json").read_text())["entries"]
    assert code == 0
    assert set(entries) == {"vid1"}


def test_cache_round_trip_and_atomic_save(tmp_path, monkeypatch):
    track = collector.track_row(_audio(1))
    path = tmp_path / "cache.json"

    collector.save_cache(path, {"vid1": track})

    assert collector.load_cache(path) == {"vid1": track}
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert set(data["entries"]["vid1"]) == {
        "track_id",
        "title",
        "artists",
        "album",
        "album_id",
        "duration_seconds",
        "thumbnail_url",
    }

    def broken_replace(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(collector.os, "replace", broken_replace)
    other = collector.track_row(_audio(2))
    with pytest.raises(OSError):
        collector.save_cache(path, {"vid2": other})
    assert collector.load_cache(path) == {"vid1": track}
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]


def test_a_missing_cache_is_empty(tmp_path):
    assert collector.load_cache(tmp_path / "cache.json") == {}


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"version": 2, "entries": {}}',
        '{"version": 1}',
        '{"version": 1, "entries": {"v": {"track_id": "x"}}}',
    ],
)
def test_unreadable_cache_exits_1(h, caplog, content):
    h.data_dir.mkdir()
    (h.data_dir / "cache.json").write_text(content)
    h.provider = _provider_for(_video(1))

    with caplog.at_level(logging.ERROR):
        code = h.run("genres")

    assert code == 1
    assert "delete it" in caplog.text
    assert h.provider.get_playlist_calls == []
    assert (h.data_dir / "cache.json").read_text() == content


# --- Input files -------------------------------------------------------------


def test_versioned_input_files_parse():
    sources = json.loads((ROOT / "ingest/device/genre_sources.json").read_text())
    overrides = json.loads((ROOT / "ingest/device/overrides.json").read_text())

    assert collector.parse_genre_sources(sources)
    assert collector.parse_overrides(overrides) == {}


def test_valid_sources_parse():
    data = {
        "genres": [
            {
                "slug": "rock",
                "playlists": [
                    {
                        "source": "PL1",
                        "title": "T",
                        "description": "D",
                        "category": "C",
                    },
                    {"source": "PL2"},
                ],
            }
        ]
    }

    [rock] = collector.parse_genre_sources(data)

    assert rock.slug == "rock"
    assert rock.playlists[0] == collector.SourcePlaylist("PL1", "T", "D", "C")
    assert rock.playlists[1] == collector.SourcePlaylist("PL2", None, None, None)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"genres": [], "extra": 1},
        {"genres": {}},
        {"genres": [{"slug": "a"}]},
        {"genres": [{"slug": "a", "playlists": [{"source": "P"}], "x": 1}]},
        {"genres": [{"slug": "a", "playlists": []}]},
        {"genres": [{"slug": "", "playlists": [{"source": "P"}]}]},
        {"genres": [{"slug": "a", "playlists": [{"source": ""}]}]},
        {"genres": [{"slug": "a", "playlists": [{"source": "P", "x": 1}]}]},
        {"genres": [{"slug": "a", "playlists": [{"title": "T"}]}]},
        {"genres": [{"slug": "a", "playlists": [{"source": "P", "title": ""}]}]},
        {
            "genres": [
                {"slug": "a", "playlists": [{"source": "P", "category": "x" * 101}]}
            ]
        },
        {
            "genres": [
                {"slug": "a", "playlists": [{"source": "P"}]},
                {"slug": "a", "playlists": [{"source": "Q"}]},
            ]
        },
        {"genres": [{"slug": "a", "playlists": [{"source": "P"}, {"source": "P"}]}]},
    ],
)
def test_sources_rejections(data):
    with pytest.raises(collector.ConfigError):
        collector.parse_genre_sources(data)


@pytest.mark.parametrize("data", [[], "x", {"a": ""}, {"": "b"}, {"a": 1}])
def test_overrides_rejections(data):
    with pytest.raises(collector.ConfigError):
        collector.parse_overrides(data)


def test_missing_overrides_raw_404_runs_normally(h):
    h.session.routes[_OVERRIDES_URL] = FakeResponse(404)
    h.provider = _provider_for(_video(1))

    assert h.run("genres") == 0
    assert "oficial=1" in h.stdout


def test_malformed_overrides_exit_1(h):
    h.session.routes[_OVERRIDES_URL] = FakeResponse(200, ["not", "an", "object"])
    h.provider = _provider_for(_video(1))

    assert h.run("genres") == 1
    assert h.provider.get_playlist_calls == []


def test_overrides_other_failure_exits_1_after_retries(h):
    h.session.routes[_OVERRIDES_URL] = requests.ConnectionError("x")

    assert h.run("genres") == 1
    assert h.sleeps == [1.0, 2.0]


def test_overrides_invalid_json_exits_1(h):
    h.session.routes[_OVERRIDES_URL] = FakeResponse(200, invalid_json=True)

    assert h.run("genres") == 1


def test_sources_404_exits_1(h):
    h.session.routes[_SOURCES_URL] = FakeResponse(404)

    assert h.run("genres") == 1


def test_fetch_retries_server_errors_then_succeeds():
    session = FakeSession()
    session.routes["u"] = [FakeResponse(503), FakeResponse(200, {"a": 1})]
    sleeps = []

    assert collector.fetch_json(
        session, "u", missing_ok=False, sleep=sleeps.append
    ) == {"a": 1}
    assert sleeps == [1.0]


# --- The submission ----------------------------------------------------------


def _payload_for(h):
    _two_songs(h)
    h.run("genres", "--write")
    return h.session.posts[0]["json"]


def test_payload_has_the_shape_of_the_back(h):
    payload = _payload_for(h)

    assert set(payload) == {"genres", "tracks"}
    [genre] = payload["genres"]
    assert genre["slug"] == "rock"
    [playlist] = genre["playlists"]
    assert playlist == {
        "source_playlist_id": "PL1",
        "title": "Source",
        "description": "Desc",
        "category": None,
        "tracks": ["aud1", "aud2"],
    }
    assert [t["track_id"] for t in payload["tracks"]] == ["aud1", "aud2"]
    assert set(payload["tracks"][0]) == {
        "track_id",
        "title",
        "artists",
        "album",
        "album_id",
        "duration_seconds",
        "thumbnail_url",
    }
    assert payload["tracks"][0]["artists"] == [{"id": "UCa", "name": "Artist A"}]


def test_payload_is_accepted_by_the_back_model(h):
    payload = _payload_for(h)

    request = GenreIngestRequest.model_validate(payload)

    assert [t.track_id for t in request.tracks] == ["aud1", "aud2"]


def test_payload_title_comes_from_the_entry_or_the_source(h):
    h.set_sources(
        [
            (
                "rock",
                [
                    {"source": "PL1", "title": "Mine", "description": "Mine too"},
                    {"source": "PL2"},
                ],
            )
        ]
    )
    h.provider = _provider_for(_video(1))
    h.provider.pages["PL2"] = {
        "title": "Theirs",
        "description": None,
        "tracks": [_video(1)],
    }

    h.run("genres", "--write")

    first, second = h.session.posts[0]["json"]["genres"][0]["playlists"]
    assert (first["title"], first["description"]) == ("Mine", "Mine too")
    assert (second["title"], second["description"]) == ("Theirs", None)


def test_payload_category_comes_from_the_entry(h):
    h.set_sources([("rock", [{"source": "PL1", "category": "Classics"}])])
    h.provider = _provider_for(_video(1))

    h.run("genres", "--write")

    assert (
        h.session.posts[0]["json"]["genres"][0]["playlists"][0]["category"]
        == "Classics"
    )


def test_payload_tracks_have_no_repeats_across_playlists(h):
    h.set_sources([("rock", [{"source": "PL1"}, {"source": "PL2"}])])
    h.provider = _provider_for(_video(1), _video(2))
    h.provider.pages["PL2"] = {"title": "Other", "tracks": [_video(2), _video(1)]}

    h.run("genres", "--write")

    payload = h.session.posts[0]["json"]
    first, second = payload["genres"][0]["playlists"]
    assert first["tracks"] == ["aud1", "aud2"]
    assert second["tracks"] == ["aud2", "aud1"]
    assert [t["track_id"] for t in payload["tracks"]] == ["aud1", "aud2"]
    GenreIngestRequest.model_validate(payload)


def test_only_saved_statuses_are_sent_and_skipped_are_counted(h):
    h.provider = _provider_for(
        _video(1),
        _video(2),
        _video(3),
        _video(4, isAvailable=False),
        answers={"vid2": [_audio(2, artists=_artists(("UCz", "Z")))], "vid3": []},
    )
    h.provider.pages["PL1"]["tracks"].append(_audio(5, videoId="vid5"))

    code = h.run("genres", "--write")

    assert code == 0
    assert h.session.posts[0]["json"]["genres"][0]["playlists"][0]["tracks"] == [
        "aud1",
        "vid5",
    ]
    line = h.stdout.splitlines()[0]
    assert "oficial=1" in line and "ya era audio=1" in line
    assert "solo por nombre=1" in line and "sin match=1" in line
    assert "datos incompletos=1" in line and "se guardan=2" in line


def test_a_playlist_without_any_title_is_a_config_error(h):
    h.provider = _provider_for(_video(1))
    h.provider.pages["PL1"]["title"] = ""

    assert h.run("genres", "--write") == 1
    assert h.session.posts == []


# --- CSV and summary ---------------------------------------------------------


def test_csv_name_header_and_one_row_per_item(h):
    h.provider = _provider_for(
        _video(1),
        _video(2, title="=cmd|' /C calc'!A0"),
        _video(3, isAvailable=False),
        answers={"vid2": []},
    )
    h.provider.pages["PL1"]["tracks"].append(_video(1, videoId="vid9"))

    h.run("genres")

    [report] = h.reports()
    assert report.name == "genre-ingest-20261110-123045.csv"
    lines = report.read_text().splitlines()
    assert lines[0] == (
        "genero,playlist,titulo,artistas,id_video,id_audio,estado,diferencia_duracion"
    )
    assert len(lines) == 1 + 4
    assert lines[1] == "rock,PL1,Song 1 (Official Video),Artist A,vid1,aud1,oficial,3"
    assert "'=cmd" in lines[2]
    assert lines[3].endswith("vid3,,datos incompletos,")
    assert lines[4].endswith("vid9,aud1,repetida,3")


def test_csv_protects_the_id_columns_and_keeps_them_readable(h):
    video = _video(1, videoId="-vidDash")
    h.provider = _provider_for(
        video, answers={"-vidDash": [_audio(1, videoId="-audDash")]}
    )

    h.run("genres")

    [report] = h.reports()
    row = next(csv.reader(report.read_text().splitlines()[1:]))
    assert row[4] == "'-vidDash"
    assert row[5] == "'-audDash"
    assert row[4].removeprefix("'") == "-vidDash"


def test_summary_has_one_line_per_playlist_and_a_total_with_eight_counters(h):
    h.set_sources([("rock", [{"source": "PL1"}, {"source": "PLbad"}])])
    h.provider = _two_songs(h).provider
    h.provider.pages["PLbad"] = requests.ConnectionError("down")

    code = h.run("genres")

    lines = h.stdout.splitlines()
    assert code == 1
    assert len(lines) == 3
    assert lines[1] == "rock PLbad: no leida"
    for status in collector.Status:
        assert f"{status.value}=" in lines[0]
        assert f"{status.value}=" in lines[2]
    assert lines[2].startswith("total: ") and lines[2].endswith("se guardan=2")


def test_summary_goes_to_out_not_to_the_logger(h, caplog, capsys):
    h.provider = _two_songs(h).provider

    with caplog.at_level(logging.DEBUG):
        h.run("genres")

    assert "total:" in h.stdout
    assert "total:" not in caplog.text
    assert "total:" not in capsys.readouterr().out


# --- The back's limits, checked before sending -------------------------------


def _many_videos(count):
    return [_video(n) for n in range(1, count + 1)]


def test_a_playlist_over_300_tracks_exits_1_naming_it_on_dry_run_and_write(h, caplog):
    h.provider = _provider_for(*_many_videos(collector.MAX_TRACKS_PER_PLAYLIST + 1))

    for argv in (("genres",), ("genres", "--write")):
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            assert h.run(*argv) == 1
        assert "'rock'" in caplog.text and "'PL1'" in caplog.text
        assert str(collector.MAX_TRACKS_PER_PLAYLIST) in caplog.text
        assert _TOKEN not in caplog.text
    assert h.session.posts == []


def test_a_playlist_of_exactly_300_tracks_is_accepted(h):
    h.provider = _provider_for(*_many_videos(collector.MAX_TRACKS_PER_PLAYLIST))

    assert h.run("genres") == 0


def test_more_than_20_playlists_in_a_genre_is_a_config_error():
    entries = [{"source": f"PL{n}"} for n in range(collector.MAX_PLAYLISTS_PER_GENRE)]
    collector.parse_genre_sources({"genres": [{"slug": "rock", "playlists": entries}]})

    entries.append({"source": "PLX"})
    with pytest.raises(collector.ConfigError, match="rock"):
        collector.parse_genre_sources(
            {"genres": [{"slug": "rock", "playlists": entries}]}
        )


def test_more_than_50_genres_is_a_config_error():
    def genres(count):
        return {
            "genres": [
                {"slug": f"g{n}", "playlists": [{"source": "P"}]} for n in range(count)
            ]
        }

    assert len(collector.parse_genre_sources(genres(collector.MAX_GENRES))) == 50
    with pytest.raises(collector.ConfigError):
        collector.parse_genre_sources(genres(collector.MAX_GENRES + 1))


@pytest.mark.parametrize(
    "genre",
    [
        {"slug": "has space", "playlists": [{"source": "P"}]},
        {"slug": "a,b", "playlists": [{"source": "P"}]},
        {"slug": "x" * 101, "playlists": [{"source": "P"}]},
        {"slug": "rock", "playlists": [{"source": "has space"}]},
        {"slug": "rock", "playlists": [{"source": "a.b"}]},
        {"slug": "rock", "playlists": [{"source": "x" * 129}]},
        {"slug": "rock", "playlists": [{"source": "P\n"}]},
    ],
)
def test_slug_and_source_outside_the_back_patterns_are_config_errors(genre):
    with pytest.raises(collector.ConfigError):
        collector.parse_genre_sources({"genres": [genre]})


def test_a_source_outside_the_pattern_exits_1_on_dry_run_without_a_provider_call(
    h, caplog
):
    h.set_sources([("rock", [{"source": "bad source"}])])

    with caplog.at_level(logging.ERROR):
        assert h.run("genres") == 1

    assert "rock" in caplog.text
    assert h.provider.get_playlist_calls == []


def _synthetic_playlist(genre, source, tracks):
    resolutions = [
        collector.Resolution(
            video_id=f"v{t}",
            title="T",
            artists="A",
            status=collector.Status.OFFICIAL,
            audio_id=t,
            track=collector.Track(t, "T", (("UCa", "A"),), "Al", "alb", 100, _COVER),
        )
        for t in tracks
    ]
    return collector.PlaylistResult(
        genre=genre,
        source=collector.SourcePlaylist(source, "T", None, None),
        read=True,
        title="T",
        resolutions=resolutions,
    )


def test_more_than_10000_distinct_tracks_is_a_config_error():
    def lists(distinct):
        ids = [f"t{n}" for n in range(distinct)]
        return [
            _synthetic_playlist("rock", f"P{i}", ids[i * 250 : (i + 1) * 250])
            for i in range(-(-distinct // 250))
        ]

    ok = collector.build_genre_payload(lists(collector.MAX_TRACKS))
    assert len(ok["tracks"]) == collector.MAX_TRACKS
    with pytest.raises(collector.ConfigError, match="10000"):
        collector.build_genre_payload(lists(collector.MAX_TRACKS + 1))


def _max_length(field):
    return next(m.max_length for m in field.metadata if hasattr(m, "max_length"))


def _annotation_constraint(annotated):
    return annotated.__metadata__[0]


def test_collector_limits_and_patterns_equal_the_backs():
    # Read from the back's module; the collector itself imports nothing of it.
    from models import ingest as back

    assert collector.MAX_TITLE == _annotation_constraint(back.Title).max_length
    assert collector.MAX_DURATION == back.MAX_DURATION_SECONDS
    assert collector.MAX_ARTISTS == back.MAX_ARTISTS_PER_TRACK
    assert collector.MAX_URL == _annotation_constraint(back.HttpsUrl).max_length
    assert collector.MAX_GENRES == back.MAX_GENRES
    assert collector.MAX_PLAYLISTS_PER_GENRE == back.MAX_PLAYLISTS_PER_GENRE
    assert collector.MAX_TRACKS_PER_PLAYLIST == back.MAX_TRACKS_PER_PLAYLIST
    assert collector.MAX_TRACKS == back.MAX_TRACKS
    provider_id = _annotation_constraint(back.ProviderId)
    assert provider_id.pattern == f"^{collector.ID_PATTERN}$"
    assert provider_id.max_length == collector.MAX_ID
    slug = _annotation_constraint(back.Slug)
    assert slug.pattern == f"^{collector.SLUG_PATTERN[:-1]}{{1,{collector.MAX_SLUG}}}$"
    fields = back.IngestGenrePlaylist.model_fields
    assert _max_length(fields["title"]) == collector.MAX_PLAYLIST_TITLE
    assert _max_length(fields["description"]) == collector.MAX_DESCRIPTION
    assert _max_length(fields["category"]) == collector.MAX_CATEGORY


# --- Exit codes --------------------------------------------------------------


def test_dry_run_sends_nothing_and_exits_0(h):
    h.provider = _two_songs(h).provider

    def refuse(*args, **kwargs):
        raise AssertionError("a dry run must not post")

    h.session.post = refuse

    assert h.run("genres") == 0
    assert len(h.reports()) == 1
    assert all(url.startswith(collector.RAW_BASE_URL) for url in h.session.gets)


def test_write_sends_once_and_exits_0(h):
    h.provider = _two_songs(h).provider

    code = h.run("genres", "--write")

    assert code == 0
    [post] = h.session.posts
    assert post["url"] == f"{_API}/ingest/genres"
    assert post["headers"] == {"Authorization": f"Bearer {_TOKEN}"}
    assert h.stdout.splitlines()[-1] == "sent: genres=1 playlists=1 tracks=2"


def test_provider_error_after_retries_exits_1_but_writes_csv_and_summary(h):
    h.provider = _provider_for(_video(1), _video(2))
    h.provider.search_answers["Song 2 (Official Video)"] = requests.ConnectionError("x")

    code = h.run("genres", "--write")

    assert code == 1
    assert h.session.posts == []
    assert len(h.reports()) == 1
    assert "error del proveedor=1" in h.stdout


def test_unreadable_source_playlist_exits_1(h):
    h.provider.pages["PL1"] = requests.ConnectionError("down")

    code = h.run("genres", "--write")

    assert code == 1
    assert h.sleeps == [1.0, 2.0]
    assert h.session.posts == []
    assert "no leida" in h.stdout
    assert len(h.reports()) == 1


def test_playlist_with_zero_saved_tracks_exits_1_without_sending(h):
    h.set_sources([("rock", [{"source": "PL1"}, {"source": "PL2"}])])
    h.provider = _provider_for(_video(1))
    h.provider.pages["PL2"] = {"title": "Bad", "tracks": [_video(2)]}

    code = h.run("genres", "--write")

    assert code == 1
    assert h.session.posts == []
    assert "se guardan=0" in h.stdout.splitlines()[1]


def test_empty_genres_exit_1(h, caplog):
    h.set_sources([])

    with caplog.at_level(logging.ERROR):
        code = h.run("genres", "--write")

    assert code == 1
    assert "nothing to ingest" in caplog.text
    assert h.provider.get_playlist_calls == []


def test_override_mismatch_exits_1_without_csv(h):
    h.session.routes[_OVERRIDES_URL] = FakeResponse(200, {"vid1": "audOV"})
    h.provider = _provider_for(_video(1))
    h.provider.watch["audOV"] = {"tracks": [_audio(5, videoId="other")]}

    assert h.run("genres") == 1
    assert h.reports() == []


def test_override_is_used_through_main(h):
    h.session.routes[_OVERRIDES_URL] = FakeResponse(200, {"vid1": "audOV"})
    h.provider = _provider_for(_video(1))
    h.provider.watch["audOV"] = {"tracks": [_audio(5, videoId="audOV")]}

    assert h.run("genres", "--write") == 0
    assert h.session.posts[0]["json"]["genres"][0]["playlists"][0]["tracks"] == [
        "audOV"
    ]
    assert h.provider.search_calls == []
    assert "excepcion manual=1" in h.stdout


@pytest.mark.parametrize(
    "argv", [[], ["releases"], ["genres", "--bogus"], ["genres", "extra"]]
)
def test_unknown_kind_and_bad_args_exit_2(h, argv, capsys):
    assert h.run(*argv) == 2
    assert h.session.gets == []


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "permissions",
        "group_readable",
        "unknown_key",
        "missing_key",
        "bad_line",
        "http_public",
    ],
)
def test_config_missing_wrong_permissions_unknown_key_or_http_exit_1(h, case):
    good = f"BEATLY_API_URL={_API}\nBEATLY_INGEST_TOKEN={_TOKEN}\n"
    if case == "missing":
        h.config_path.unlink()
    elif case == "permissions":
        h.write_config(mode=0o644)
    elif case == "group_readable":
        h.write_config(mode=0o640)
    elif case == "unknown_key":
        h.write_config(good + "OTHER=1\n")
    elif case == "missing_key":
        h.write_config(f"BEATLY_API_URL={_API}\n")
    elif case == "bad_line":
        h.write_config(good + "garbage\n")
    elif case == "http_public":
        h.write_config(
            f"BEATLY_API_URL=http://api.example.com\nBEATLY_INGEST_TOKEN={_TOKEN}\n"
        )

    assert h.run("genres") == 1
    assert h.session.gets == []
    assert h.provider.get_playlist_calls == []


def test_config_ignores_comments_blank_lines_and_quotes(tmp_path):
    path = tmp_path / "c.env"
    path.write_text(
        f"# comment\n\nBEATLY_API_URL=\"{_API}/\"\nBEATLY_INGEST_TOKEN='{_TOKEN}'\n"
    )
    os.chmod(path, 0o600)

    config = collector.read_config(path)

    assert config.api_url == _API
    assert config.token == _TOKEN
    assert _TOKEN not in repr(config)


def test_main_never_reads_stdin(h, monkeypatch):
    class _NoStdin:
        def read(self, *args):
            raise AssertionError("stdin was read")

        readline = read

        def __iter__(self):
            raise AssertionError("stdin was read")

    monkeypatch.setattr(sys, "stdin", _NoStdin())
    h.provider = _two_songs(h).provider

    assert h.run("genres", "--write") == 0


def test_data_dir_is_created_with_mode_0700(h):
    h.provider = _two_songs(h).provider

    h.run("genres")

    assert h.data_dir.stat().st_mode & 0o777 == 0o700


# --- BEATLY_API_URL ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com",
        "https://api.example.com/",
        "https://api.example.com:8443/base",
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://10.0.0.1",
        "http://10.255.255.255:8000",
        "http://172.16.0.1",
        "http://172.31.255.255",
        "http://192.168.0.1",
        "http://192.168.1.50:8000",
    ],
)
def test_api_url_accepted(url):
    assert collector.validate_api_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com",
        "http://8.8.8.8",
        "http://11.0.0.1",
        "http://172.15.255.255",
        "http://172.32.0.1",
        "http://192.167.255.255",
        "http://192.169.0.1",
        "http://localhost.evil.com",
        "http://10.0.0.1.evil.com",
        "http://[::1]:8000",
        "http://localhost@evil.com",
        "https://user:pass@api.example.com",
        "ftp://api.example.com",
        "api.example.com",
        "https://",
        "https://api.example.com:notaport",
    ],
)
def test_api_url_rejected(url):
    with pytest.raises(collector.ConfigError) as raised:
        collector.validate_api_url(url)

    assert "pass" not in str(raised.value)
    assert "evil" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://10.1.2.3:8000",
        "http://172.16.5.5",
        "http://192.168.1.7:8000",
    ],
)
def test_http_to_localhost_and_private_ranges_runs_through_main(h, url):
    h.write_config(f"BEATLY_API_URL={url}\nBEATLY_INGEST_TOKEN={_TOKEN}\n")
    _two_songs(h)

    assert h.run("genres", "--write") == 0
    assert h.session.posts[0]["url"] == f"{url}/ingest/genres"


def test_https_runs_through_main(h):
    h.provider = _two_songs(h).provider

    assert h.run("genres", "--write") == 0


@pytest.mark.parametrize(
    "url", ["http://example.com", "http://172.15.0.1", "http://172.32.0.1"]
)
def test_http_to_a_public_host_exits_1(h, url, caplog):
    h.write_config(f"BEATLY_API_URL={url}\nBEATLY_INGEST_TOKEN={_TOKEN}\n")
    h.provider = _two_songs(h).provider

    with caplog.at_level(logging.ERROR):
        code = h.run("genres", "--write")

    assert code == 1
    assert "must use https" in caplog.text
    assert url not in caplog.text
    assert h.session.posts == []
    assert h.session.gets == []


# --- The send ----------------------------------------------------------------


def _payload():
    return {
        "genres": [{"slug": "rock", "playlists": [{"tracks": ["a", "b"]}]}],
        "tracks": [],
    }


def _config():
    return collector.Config(api_url=_API, token=_TOKEN)


def test_send_retries_network_errors_and_5xx_then_succeeds():
    session = FakeSession()
    ok = FakeResponse(200, {"ok": True, "data": {"genres": 1}})
    session.post_script = [
        requests.ConnectionError("x"),
        requests.Timeout("t"),
        FakeResponse(502),
        ok,
    ]
    sleeps = []

    data = collector.send(
        session, _config(), "/ingest/genres", _payload(), sleep=sleeps.append
    )

    assert data == {"genres": 1}
    assert len(session.posts) == 4
    assert sleeps == [5, 15, 45]


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ChunkedEncodingError("cut"),
        requests.exceptions.ContentDecodingError("bad"),
        requests.exceptions.RequestException("other"),
    ],
)
def test_send_retries_every_requests_error_and_logs_it_without_the_token(error, caplog):
    session = FakeSession()
    session.post_script = [error, FakeResponse(200, {"data": {"genres": 1}})]
    sleeps = []

    with caplog.at_level(logging.DEBUG):
        data = collector.send(
            session, _config(), "/ingest/genres", _payload(), sleep=sleeps.append
        )

    assert data == {"genres": 1}
    assert sleeps == [5]
    assert type(error).__name__ in caplog.text
    assert _TOKEN not in caplog.text


def test_send_exhausted_by_a_chunked_error_exits_1_with_it_in_the_log(h, caplog):
    h.provider = _two_songs(h).provider
    h.session.post_script = [requests.exceptions.ChunkedEncodingError(_TOKEN)]

    with caplog.at_level(logging.DEBUG):
        assert h.run("genres", "--write") == 1

    assert len(h.session.posts) == 4
    assert "ChunkedEncodingError" in caplog.text
    assert _TOKEN not in caplog.text


@pytest.mark.parametrize("status", [502, 503, 504])
def test_send_retries_each_gateway_status(status):
    session = FakeSession()
    session.post_script = [FakeResponse(status), FakeResponse(200, {"data": {}})]
    sleeps = []

    collector.send(
        session, _config(), "/ingest/genres", _payload(), sleep=sleeps.append
    )

    assert len(session.posts) == 2
    assert sleeps == [5]


def test_send_gives_up_after_four_attempts_and_exits_1(h):
    h.provider = _two_songs(h).provider
    h.session.post_script = [FakeResponse(503)]

    code = h.run("genres", "--write")

    assert code == 1
    assert len(h.session.posts) == 4
    assert h.sleeps == [5, 15, 45]


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "unauthorized"), (404, "genre_not_found"), (422, "invalid_request")],
)
def test_send_does_not_retry_4xx(h, caplog, status, reason):
    h.provider = _two_songs(h).provider
    h.session.post_script = [FakeResponse(status, {"ok": False, "reason": reason})]

    with caplog.at_level(logging.ERROR):
        code = h.run("genres", "--write")

    assert code == 1
    assert len(h.session.posts) == 1
    assert h.sleeps == []
    assert f"status={status}" in caplog.text
    assert f"reason={reason}" in caplog.text


def test_send_logs_unknown_for_a_reason_that_is_not_an_identifier(h, caplog):
    h.provider = _two_songs(h).provider
    h.session.post_script = [
        FakeResponse(422, {"reason": "Some free text, with detail"})
    ]

    with caplog.at_level(logging.ERROR):
        h.run("genres", "--write")

    assert "reason=unknown" in caplog.text
    assert "free text" not in caplog.text


def test_send_response_with_other_counts_exits_1(h):
    h.provider = _two_songs(h).provider
    h.session.post_script = [
        FakeResponse(
            200, {"ok": True, "data": {"genres": 1, "playlists": 1, "tracks": 1}}
        )
    ]

    assert h.run("genres", "--write") == 1
    assert len(h.session.posts) == 1


@pytest.mark.parametrize("payload", [None, [], {"ok": True}, {"data": []}])
def test_send_response_with_another_shape_exits_1(h, payload):
    h.provider = _two_songs(h).provider
    h.session.post_script = [FakeResponse(200, payload)]

    assert h.run("genres", "--write") == 1


def test_send_uses_bearer_and_never_logs_the_token(h, caplog, capsys):
    h.provider = _two_songs(h).provider
    h.session.post_script = [
        requests.ConnectionError(_TOKEN),
        FakeResponse(401, {"reason": "unauthorized"}),
    ]

    with caplog.at_level(logging.DEBUG):
        h.run("genres", "--write")

    captured = capsys.readouterr()
    everything = caplog.text + captured.out + captured.err
    everything += h.stdout + "".join(p.read_text() for p in h.reports())
    assert _TOKEN not in everything
    assert h.session.posts[0]["headers"] == {"Authorization": f"Bearer {_TOKEN}"}


# --- Isolation ---------------------------------------------------------------


def _imports(path):
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name.partition(".")[0], 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append(((node.module or "").partition(".")[0], node.level))
    return found


def _provider_root():
    # The third-party import of core/search_provider.py: read from there so
    # this test does not write the name.
    roots = {
        root
        for root, level in _imports(ROOT / "core/search_provider.py")
        if level == 0 and root not in sys.stdlib_module_names
    }
    roots -= {"core", "models", "routes", "services"}
    assert len(roots) == 1
    return roots.pop()


def test_collector_imports_only_stdlib_requests_and_the_provider_library():
    allowed = set(sys.stdlib_module_names) | {"requests", _provider_root()}
    imports = _imports(ROOT / "ingest/device/collector.py")

    assert imports
    for root, level in imports:
        assert level == 0, "relative import"
        assert root in allowed, f"unexpected import: {root}"
    roots = {root for root, _ in imports}
    assert not roots & {
        "core",
        "models",
        "routes",
        "services",
        "scripts",
        "app",
        "pydantic",
        "supabase",
    }


def test_the_provider_library_name_appears_only_in_import_lines():
    needle = _provider_root().lower().removesuffix("api")
    lines = (ROOT / "ingest/device/collector.py").read_text().splitlines()

    offending = [
        line
        for line in lines
        if needle in line.lower() and not line.startswith(("from ", "import "))
    ]

    assert offending == []


def test_collector_parses_as_python_3_11():
    source = (ROOT / "ingest/device/collector.py").read_text()

    tree = ast.parse(source, feature_version=(3, 11))

    first = tree.body[0]
    # After the module comment, `from __future__ import annotations` is the
    # first statement.
    assert isinstance(first, ast.ImportFrom)
    assert first.module == "__future__"
    assert [a.name for a in first.names] == ["annotations"]


def test_device_requirements_match_the_back():
    device = (ROOT / "ingest/device/requirements.txt").read_text().split()
    back = set((ROOT / "requirements.txt").read_text().split())

    assert device
    assert all(line in back for line in device)
    names = {line.partition("==")[0].lower() for line in device}
    assert names == {
        "requests",
        _provider_root().lower(),
        "urllib3",
        "idna",
        "certifi",
        "charset-normalizer",
    }


def test_collector_has_no_print_and_no_setup_logging():
    tree = ast.parse((ROOT / "ingest/device/collector.py").read_text())
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert "print" not in calls
    assert "setup_logging" not in calls
