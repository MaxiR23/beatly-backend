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
# - Six searches at most in flight; a transient provider error is retried (5
#   attempts, waits with bases 2, 4, 8 and 16 s, each a random value between its
#   half and itself) and an exhausted one is "error del proveedor" and is not
#   cached; the input files are fetched with 1 s and 2 s and no jitter; the
#   second pass (60 s, one by one) over the songs left as provider errors; the
#   breaker that stops asking the provider after 10 exhausted calls (it counts
#   playlist reads and the second pass, refuses without a request, skips the
#   second pass and still sends the complete genres); the cache holds only
#   "oficial", is written atomically, a second run with it makes no search, and
#   an unreadable one exits 1
# - The input files: the versioned ones parse, rejections, a 404 for the
#   overrides is "no exceptions", a malformed one exits 1
# - The submission: the shape the back expects (and the back's own model
#   accepts it), the title of the entry or of the source, no repeated track
#   across playlists, only the saved statuses go and the skipped are counted
# - The back's limits and patterns, checked before sending: 500 tracks per
#   playlist, 50 playlists per genre, 50 genres, 20 000 distinct tracks, the
#   id and slug patterns; an input over one exits 1 naming genre, playlist and
#   limit, on a dry run too, and sends nothing; the collector's constants and
#   patterns equal the ones in models/ingest.py (read, never imported by it)
# - The CSV (name, header, one row per item, every column formula-safe, ids
#   included) and the summary
#   (one line per playlist and a total with the eight counters, on out and not
#   in the log)
# - Exit codes: a dry run sends nothing and exits 0; --write posts once and
#   exits 0; a genre with a provider error, an unread playlist or a playlist
#   with no saved song is left out and the rest is sent, exit 3 (on a dry run
#   too); with no complete genre, or if the send fails or the counts differ, it
#   exits 1 without sending (or after) but with CSV and summary; the limits
#   count only what is sent; no genres, bad arguments (2) and bad config exit
#   as they should; stdin is never read
# - BEATLY_API_URL: https accepted; http only to localhost, 127.0.0.1 and the
#   private ranges (172.15 and 172.32 rejected); a public host over http exits 1
# - The send: retries network errors and 502/503/504 with waits of 5, 15 and
#   45 s, gives up after four attempts, never retries a 4xx, rejects other
#   counts, and never logs the token
# - Isolation: the file imports only the standard library, requests and the
#   provider library, parses as Python 3.11, and its device requirements are
#   the back's pinned lines
#
# - The kind releases: one browse call to the page with the language fixed to
#   "en" (and the page retried), the rules of a card in order with the first
#   failing one as the reason, the artists without a link that discard the whole
#   card, the repeated albums, the CSV (name, header, formula-safe cells) and the
#   summary, the aborts (page not read, unexpected shape, no cards, none passing,
#   under MIN_KEPT_RATIO of the cards read, over the back's limit) with the CSV
#   and no sending, a dry run that sends nothing and downloads no input file, a
#   write that posts once, never exits 3, and the limits and patterns equal to
#   the back's
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
import random
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
import requests

from ingest.device import collector
from models.ingest import GenreIngestRequest, ReleaseIngestRequest

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


def _full(lo, hi):
    # The jitter source of the tests: the whole base, so the waits are exact.
    return hi


def _half(lo, hi):
    return lo


_BASES = [
    collector.PROVIDER_BASE_DELAY * 2**n for n in range(collector.PROVIDER_ATTEMPTS - 1)
]


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
        self.browse_pages: list[object] = []
        self.browse_calls: list[tuple] = []
        # A title to the answers of its searches: consumed in order, the last
        # one repeats; an exception is raised.
        self.search_scripts: dict[str, list] = {}

    def _send_request(self, endpoint, body):
        # The private method the releases kind reads the raw page with.
        self.browse_calls.append((endpoint, dict(body)))
        page = (
            self.browse_pages.pop(0)
            if len(self.browse_pages) > 1
            else (self.browse_pages[0])
        )
        if isinstance(page, Exception):
            raise page
        return page

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
            scripted = next(
                (t for t in self.search_scripts if query.startswith(t)), None
            )
            step = None
            if scripted is not None:
                script = self.search_scripts[scripted]
                step = script.pop(0) if len(script) > 1 else script[0]
        try:
            if isinstance(step, Exception):
                raise step
            if step is not None:
                return step
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


def _resolver(
    provider=None, overrides=None, cache=None, sleeps=None, uniform=_full, breaker=None
):
    sleeps = sleeps if sleeps is not None else []
    return collector.Resolver(
        provider or FakeProvider(),
        overrides=overrides or {},
        cache=cache if cache is not None else {},
        sleep=sleeps.append,
        uniform=uniform,
        breaker=breaker,
    )


def _open_breaker():
    breaker = collector.ProviderBreaker()
    for _ in range(collector.PROVIDER_ERROR_LIMIT):
        breaker.failed()
    return breaker


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
        self.uniform = _full
        self.breaker = None
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
            uniform=self.uniform,
            breaker=self.breaker,
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
    assert sleeps == _BASES


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
        uniform=_full,
        breaker=collector.ProviderBreaker(),
    )

    collector.resolve_all(_resolver(provider), h_playlists, sleep=lambda s: None)

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
        uniform=_full,
        breaker=collector.ProviderBreaker(),
    )

    collector.resolve_all(_resolver(provider), playlists, sleep=lambda s: None)

    assert len(provider.search_calls) == 20
    assert 1 < provider.max_in_flight <= 6


def test_transient_provider_error_is_retried_then_resolves():
    provider = FakeProvider()
    provider.search_answers["Song 1"] = [_audio(1)]
    provider.search_errors = [requests.ConnectionError("x"), ValueError("layout")]
    sleeps = []

    result = _resolver(provider, sleeps=sleeps).resolve(_video(1))

    assert result.status is collector.Status.OFFICIAL
    assert sleeps == _BASES[:2]
    assert len(provider.search_calls) == 3


def test_exhausted_retries_give_provider_error_and_nothing_is_cached(h):
    h.provider = _provider_for(_video(1))
    h.provider.search_errors = [requests.Timeout("t")] * (
        2 * collector.PROVIDER_ATTEMPTS
    )

    code = h.run("genres", "--write")

    assert code == 1
    assert h.sleeps == _BASES + [collector.SECOND_PASS_WAIT] + _BASES
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
    assert len(lines) == 4
    assert lines[1] == "rock PLbad: no leida"
    for status in collector.Status:
        assert f"{status.value}=" in lines[0]
        assert f"{status.value}=" in lines[2]
    assert lines[2].startswith("total: ") and lines[2].endswith("se guardan=2")
    assert lines[3] == "no se envia rock: 1 source playlists not read"


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


def test_more_than_the_maximum_distinct_tracks_is_a_config_error():
    def lists(distinct):
        ids = [f"t{n}" for n in range(distinct)]
        return [
            _synthetic_playlist("rock", f"P{i}", ids[i * 250 : (i + 1) * 250])
            for i in range(-(-distinct // 250))
        ]

    ok = collector.build_genre_payload(lists(collector.MAX_TRACKS))
    assert len(ok["tracks"]) == collector.MAX_TRACKS
    with pytest.raises(collector.ConfigError, match=str(collector.MAX_TRACKS)):
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
    assert h.sleeps == _BASES
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
    "argv", [[], ["albums"], ["genres", "--bogus"], ["genres", "extra"]]
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


# --- Releases ----------------------------------------------------------------

_ARTIST_TYPE = "MUSIC_PAGE_TYPE_ARTIST"
_RELEASES_BROWSE = ("browse", {"browseId": "FEmusic_new_releases_albums"})


def _run_(text, artist_id=None, page_type=_ARTIST_TYPE):
    # A run of the subtitle: with a link when it has an artist id.
    run = {"text": text}
    if artist_id is not None:
        run["navigationEndpoint"] = {
            "browseEndpoint": {
                "browseId": artist_id,
                "browseEndpointContextSupportedConfigs": {
                    "browseEndpointContextMusicConfig": {"pageType": page_type}
                },
            }
        }
    return run


def _artist_id(n=1):
    return "UC" + f"{n:022d}"


def _card(n=1, **overrides):
    # The reference card: an Album by one linked artist, as measured.
    parts = {
        "album_id": "MPREb_" + f"{n:011d}",
        "playlist_id": "OLAK5uy_" + f"{n:033d}",
        "title": f"Album {n}",
        "subtitle": [
            _run_("Album"),
            _run_(" \u2022 "),
            _run_(f"Artist {n}", _artist_id(n)),
        ],
        "thumbnails": [
            {"url": "https://example.com/a=w226-h226-l90-rj", "width": 226},
            {"url": "https://example.com/a=w544-h544-l90-rj", "width": 544},
        ],
        **overrides,
    }
    return {
        "musicTwoRowItemRenderer": {
            "title": {"runs": [{"text": parts["title"]}]},
            "subtitle": {"runs": parts["subtitle"]},
            "navigationEndpoint": {"browseEndpoint": {"browseId": parts["album_id"]}},
            "thumbnailRenderer": {
                "musicThumbnailRenderer": {
                    "thumbnail": {"thumbnails": parts["thumbnails"]}
                }
            },
            "thumbnailOverlay": {
                "musicItemThumbnailOverlayRenderer": {
                    "content": {
                        "musicPlayButtonRenderer": {
                            "playNavigationEndpoint": {
                                "watchPlaylistEndpoint": {
                                    "playlistId": parts["playlist_id"]
                                }
                            }
                        }
                    }
                }
            },
        }
    }


def _releases_page(*cards, sections=None, grid_extra=None):
    grid = {"gridRenderer": {"items": list(cards), **(grid_extra or {})}}
    contents = sections if sections is not None else [grid]
    return {
        "contents": {
            "singleColumnBrowseResultsRenderer": {
                "tabs": [
                    {
                        "tabRenderer": {
                            "content": {"sectionListRenderer": {"contents": contents}}
                        }
                    }
                ]
            }
        }
    }


class ReleaseSession(FakeSession):
    # Accepts with the counts of the releases payload when nothing is scripted.
    def post(self, url, json=None, headers=None, timeout=None):
        if self.post_script:
            return super().post(url, json=json, headers=headers, timeout=timeout)
        self.posts.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(200, {"ok": True, "data": collector.release_counts(json)})


@pytest.fixture
def hr(h):
    h.session = ReleaseSession()
    h.provider.browse_pages = [_releases_page(_card(1), _card(2))]
    return h


def _saved(card):
    parsed = collector.parse_release_card(card)
    assert parsed.reason is None, parsed.reason
    return parsed


def test_releases_dry_run_and_write_are_valid_kinds(hr):
    assert hr.run("releases") == 0
    assert hr.run("releases", "--write") == 0


def test_releases_builds_the_provider_with_the_fixed_language(hr, monkeypatch):
    calls = []

    class RecordingProvider(FakeProvider):
        def __init__(self, **kwargs):
            super().__init__()
            calls.append(kwargs)
            self.browse_pages = [_releases_page(_card(1))]

    monkeypatch.setattr(collector, "Provider", RecordingProvider)
    hr.provider = None

    assert hr.run("releases") == 0
    assert len(calls) == 1
    assert calls[0]["language"] == "en"
    assert "location" not in calls[0]


def test_build_provider_without_language_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(collector, "Provider", lambda **kwargs: calls.append(kwargs))

    collector.build_provider()

    assert list(calls[0]) == ["requests_session"]


def test_releases_reads_the_page_once_with_browse(hr):
    assert hr.run("releases") == 0
    assert hr.provider.browse_calls == [_RELEASES_BROWSE]


def test_releases_page_is_retried_then_read(hr):
    hr.provider.browse_pages = [
        requests.ConnectionError("down"),
        _releases_page(_card(1)),
    ]

    assert hr.run("releases") == 0
    assert hr.sleeps == _BASES[:1]
    assert len(hr.provider.browse_calls) == 2


def test_a_valid_card_parses_to_the_release():
    card = _saved(
        _card(
            3,
            title="  Some Album ",
            subtitle=[
                _run_("EP"),
                _run_(" \u2022 "),
                _run_(" Artist A ", _artist_id(1)),
                _run_(" & "),
                _run_("Artist B", _artist_id(2)),
            ],
        )
    )

    assert card.release.to_row() == {
        "album_id": "MPREb_" + f"{3:011d}",
        "audio_playlist_id": "OLAK5uy_" + f"{3:033d}",
        "type": "ep",
        "title": "Some Album",
        "artists": [
            {"id": _artist_id(1), "name": "Artist A"},
            {"id": _artist_id(2), "name": "Artist B"},
        ],
        "thumbnail_url": "https://example.com/a=w544-h544-l90-rj",
    }


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"album_id": "XXXXb_123"}, collector.Discard.ALBUM_ID),
        ({"album_id": "MPREb_" + "a" * 123}, collector.Discard.ALBUM_ID),
        ({"playlist_id": "PLxyz"}, collector.Discard.AUDIO_PLAYLIST),
        ({"playlist_id": None}, collector.Discard.AUDIO_PLAYLIST),
        (
            {"subtitle": [_run_("Song"), _run_(" \u2022 "), _run_("A", _artist_id())]},
            collector.Discard.TYPE,
        ),
        (
            {"subtitle": [_run_("Ep"), _run_(" \u2022 "), _run_("A", _artist_id())]},
            collector.Discard.TYPE,
        ),
        (
            {"subtitle": [_run_("album"), _run_(" \u2022 "), _run_("A", _artist_id())]},
            collector.Discard.TYPE,
        ),
        ({"title": ""}, collector.Discard.TITLE),
        ({"title": "   "}, collector.Discard.TITLE),
        ({"title": "t" * 301}, collector.Discard.TITLE),
        (
            {"subtitle": [_run_("Album"), _run_(" \u2022 ")]},
            collector.Discard.NO_ARTISTS,
        ),
        (
            {"subtitle": [_run_("Album"), _run_(" \u2022 "), _run_("No link")]},
            collector.Discard.NO_ARTISTS,
        ),
        (
            {
                "subtitle": [
                    _run_("Album"),
                    _run_(" \u2022 "),
                    _run_("A", "UC" + "a" * 21),
                ]
            },
            collector.Discard.ARTIST_ID,
        ),
        (
            {
                "subtitle": [
                    _run_("Album"),
                    _run_(" \u2022 "),
                    _run_("A", _artist_id(), page_type="MUSIC_PAGE_TYPE_USER_CHANNEL"),
                ]
            },
            collector.Discard.ARTIST_ID,
        ),
        (
            {
                "subtitle": [_run_("Album"), _run_(" \u2022 ")]
                + [_run_(f"A{i}", _artist_id(i)) for i in range(1, 22)]
            },
            collector.Discard.TOO_MANY_ARTISTS,
        ),
        (
            {
                "subtitle": [
                    _run_("Album"),
                    _run_(" \u2022 "),
                    _run_("A", _artist_id()),
                    _run_(" & "),
                    _run_("B with no link"),
                ]
            },
            collector.Discard.ARTIST_UNLINKED,
        ),
        *[
            (
                {
                    "subtitle": [
                        _run_("Album"),
                        _run_(" \u2022 "),
                        _run_(name, _artist_id()),
                    ]
                },
                collector.Discard.ARTIST_NAME,
            )
            for name in ("NULL", " none ", "Undefined", "", "n" * 301)
        ],
        ({"thumbnails": [{"url": "http://example.com/a"}]}, collector.Discard.COVER),
        ({"thumbnails": []}, collector.Discard.COVER),
        (
            {"thumbnails": [{"url": "https://example.com/" + "a" * 2030}]},
            collector.Discard.COVER,
        ),
    ],
)
def test_release_card_rules(overrides, reason):
    parsed = collector.parse_release_card(_card(1, **overrides))

    assert parsed.reason is reason
    assert parsed.release is None
    assert not parsed.saved


@pytest.mark.parametrize("item", [{"otherRenderer": {}}, "text", None, {}])
def test_an_unreadable_card_is_discarded(item):
    parsed = collector.parse_release_card(item)

    assert parsed.reason is collector.Discard.UNREADABLE
    assert parsed.release is None


def test_the_first_failing_rule_is_the_reported_reason():
    # The album id (a) and the cover (h) both fail: the reason is (a).
    parsed = collector.parse_release_card(
        _card(1, album_id="bad", thumbnails=[{"url": "http://example.com/a"}])
    )

    assert parsed.reason is collector.Discard.ALBUM_ID


@pytest.mark.parametrize(
    ("separators", "count"),
    [
        ([" \u2022 "], 1),
        ([" \u2022 ", " & "], 2),
        ([" \u2022 ", ", ", " & "], 3),
        ([" \u2022 ", ", ", ", ", " & "], 4),
    ],
)
def test_separators_are_not_artists(separators, count):
    subtitle = [_run_("Album"), _run_(separators[0])]
    for i in range(1, count + 1):
        subtitle.append(_run_(f"Artist {i}", _artist_id(i)))
        if i < count:
            subtitle.append(_run_(separators[i]))

    release = _saved(_card(1, subtitle=subtitle)).release

    assert [name for _, name in release.artists] == [
        f"Artist {i}" for i in range(1, count + 1)
    ]


def test_a_card_with_an_unlinked_artist_is_discarded_whole():
    # The measured shape ttLtt: the second artist has no link.
    parsed = collector.parse_release_card(
        _card(
            1,
            subtitle=[
                _run_("Album"),
                _run_(" \u2022 "),
                _run_("Artist A", _artist_id()),
                _run_(" & "),
                _run_("Artist B"),
            ],
        )
    )

    assert parsed.reason is collector.Discard.ARTIST_UNLINKED
    assert parsed.artist_names == ("Artist A", "Artist B")


def test_a_repeated_album_keeps_the_first_and_marks_the_rest():
    cards = collector.mark_repeated_releases(
        [
            collector.parse_release_card(_card(1)),
            collector.parse_release_card(_card(2)),
            collector.parse_release_card(_card(1, title="Other")),
        ]
    )

    assert [c.reason for c in cards] == [None, None, collector.Discard.REPEATED]
    assert [c.saved for c in cards] == [True, True, False]


def test_release_csv_name_header_and_one_row_per_card(hr):
    hr.provider.browse_pages = [
        _releases_page(_card(1), _card(2, title=""), _card(1), _card(3))
    ]

    assert hr.run("releases") == 0
    (path,) = hr.reports()

    assert path.name == "release-ingest-20261110-123045.csv"
    rows = list(csv.reader(path.open(newline="", encoding="utf-8")))
    assert rows[0] == list(collector.RELEASE_CSV_HEADER)
    assert [r[:3] for r in rows[1:]] == [
        ["1", "se guarda", "Album"],
        ["2", "titulo invalido", "Album"],
        ["3", "repetido", "Album"],
        ["4", "se guarda", "Album"],
    ]
    assert rows[1][4] == "Artist 1"
    assert rows[1][5] == "MPREb_" + f"{1:011d}"


def test_release_csv_cells_are_formula_safe(hr):
    hr.provider.browse_pages = [
        _releases_page(
            _card(1, title="=HYPERLINK(1)", album_id="-bad"),
            _card(2),
        )
    ]

    hr.run("releases")
    (path,) = hr.reports()
    rows = list(csv.reader(path.open(newline="", encoding="utf-8")))

    assert rows[1][3] == "'=HYPERLINK(1)"
    assert rows[1][5] == "'-bad"


def test_release_summary_counts_read_saved_and_each_reason(hr, caplog):
    hr.provider.browse_pages = [
        _releases_page(_card(1), _card(2), _card(3, title=""), _card(1), "text")
    ]

    with caplog.at_level(logging.DEBUG):
        hr.run("releases")

    line = hr.stdout.splitlines()[0]
    assert line.startswith("total: leidas=5 ")
    assert "titulo invalido=1" in line
    assert "repetido=1" in line
    assert "tarjeta ilegible=1" in line
    assert "sin artistas=0" in line
    assert line.endswith("se guardan=2")
    assert "leidas=" not in caplog.text


def test_releases_unreadable_page_exits_1_without_csv(hr):
    hr.provider.browse_pages = [requests.ConnectionError("down")]

    assert hr.run("releases", "--write") == 1
    assert len(hr.provider.browse_calls) == collector.PROVIDER_ATTEMPTS
    assert hr.sleeps == _BASES
    assert hr.session.posts == []
    assert hr.reports() == []
    assert "leidas=0" in hr.stdout


@pytest.mark.parametrize(
    "page",
    [
        _releases_page(sections=[]),
        _releases_page(sections=[{"gridRenderer": {"items": []}}] * 2),
        _releases_page(sections=[{"musicShelfRenderer": {}}]),
        _releases_page(_card(1), grid_extra={"continuations": [{}]}),
        _releases_page(sections=[{"gridRenderer": {"items": "x"}}]),
        {},
        "text",
    ],
)
def test_releases_unexpected_shape_exits_1(hr, page):
    hr.provider.browse_pages = [page]

    assert hr.run("releases", "--write") == 1
    assert len(hr.provider.browse_calls) == 1
    assert hr.sleeps == []
    assert hr.session.posts == []
    assert hr.reports() == []
    assert "leidas=0" in hr.stdout


def test_releases_no_cards_exits_1(hr):
    hr.provider.browse_pages = [_releases_page()]

    assert hr.run("releases", "--write") == 1
    assert hr.session.posts == []
    assert hr.reports() == []
    assert "leidas=0" in hr.stdout


def test_releases_no_card_passing_exits_1_with_csv(hr):
    hr.provider.browse_pages = [_releases_page(_card(1, title=""), _card(2, title=""))]

    assert hr.run("releases", "--write") == 1
    assert hr.session.posts == []
    assert len(hr.reports()) == 1
    assert "se guardan=0" in hr.stdout
    assert "no card passed" in hr.stdout


def test_releases_over_the_limit_exits_1_with_csv(hr):
    count = collector.MAX_RELEASES + 1
    hr.provider.browse_pages = [
        _releases_page(*[_card(n) for n in range(1, count + 1)])
    ]

    assert hr.run("releases", "--write") == 1
    assert hr.session.posts == []
    assert len(hr.reports()) == 1
    assert f"{count} releases pass" in hr.stdout


def test_min_kept_ratio_is_half():
    assert collector.MIN_KEPT_RATIO == 0.5


def test_releases_under_the_kept_ratio_abort_with_the_figures_and_csv(hr):
    # 10 read, 4 valid: 40% is under the minimum.
    cards = [_card(n) for n in range(1, 5)] + [_card(n, title="") for n in range(5, 11)]
    hr.provider.browse_pages = [_releases_page(*cards)]

    assert hr.run("releases", "--write") == 1
    assert hr.session.posts == []
    assert len(hr.reports()) == 1
    assert "leidas=10" in hr.stdout
    assert "se guardan=4" in hr.stdout
    assert "only 4 of 10 cards pass (40.0%), under the minimum of 50%" in hr.stdout


def test_releases_exactly_at_the_kept_ratio_do_not_abort(hr):
    # 10 read, 5 valid: exactly half is accepted.
    cards = [_card(n) for n in range(1, 6)] + [_card(n, title="") for n in range(6, 11)]
    hr.provider.browse_pages = [_releases_page(*cards)]

    assert hr.run("releases", "--write") == 0
    assert len(hr.session.posts[0]["json"]["releases"]) == 5
    assert "aborta" not in hr.stdout


def test_releases_dry_run_sends_nothing_and_exits_0(hr):
    class NoPost(ReleaseSession):
        def post(self, *args, **kwargs):
            raise AssertionError("a dry run posted")

    hr.session = NoPost()

    assert hr.run("releases") == 0
    assert hr.session.gets == []
    assert len(hr.reports()) == 1


def test_releases_write_sends_once_and_exits_0(hr):
    assert hr.run("releases", "--write") == 0

    (post,) = hr.session.posts
    assert post["url"] == f"{_API}/ingest/releases"
    assert post["headers"] == {"Authorization": f"Bearer {_TOKEN}"}
    assert [r["album_id"] for r in post["json"]["releases"]] == [
        "MPREb_" + f"{1:011d}",
        "MPREb_" + f"{2:011d}",
    ]
    assert hr.stdout.splitlines()[-1] == "sent: releases=2"


def test_releases_send_is_retried_on_a_gateway_status(hr):
    hr.session.post_script = [
        FakeResponse(502, {"reason": "upstream_error"}),
        FakeResponse(200, {"ok": True, "data": {"releases": 2}}),
    ]

    assert hr.run("releases", "--write") == 0
    assert len(hr.session.posts) == 2
    assert 5 in hr.sleeps


def test_releases_other_count_exits_1(hr):
    hr.session.post_script = [FakeResponse(200, {"ok": True, "data": {"releases": 1}})]

    assert hr.run("releases", "--write") == 1
    assert len(hr.session.posts) == 1


def test_release_payload_is_accepted_by_the_back_model(hr):
    hr.run("releases", "--write")

    request = ReleaseIngestRequest.model_validate(hr.session.posts[0]["json"])

    assert len(request.releases) == 2


def test_collector_release_limits_and_patterns_equal_the_backs():
    from models import ingest as back
    from models.releases import ReleaseType

    assert collector.MAX_RELEASES == back.MAX_RELEASES
    assert collector.MAX_ARTISTS == back.MAX_ARTISTS_PER_RELEASE
    for name, annotated, pattern in (
        ("album", back.ReleaseAlbumId, collector.ALBUM_ID_PATTERN),
        ("playlist", back.ReleaseAudioPlaylistId, collector.AUDIO_PLAYLIST_PATTERN),
    ):
        constraint = _annotation_constraint(annotated)
        assert constraint.pattern == f"^{pattern}$", name
        assert constraint.max_length == collector.MAX_ID, name
    artist = _annotation_constraint(back.ReleaseArtistId)
    assert artist.pattern == f"^{collector.ARTIST_ID_PATTERN}$"
    assert set(collector.RELEASE_TYPES.values()) == set(get_args(ReleaseType))
    assert collector.FORBIDDEN_ARTIST_NAMES == back.FORBIDDEN_ARTIST_NAMES


def test_releases_threshold_message_never_equals_the_minimum_when_aborting(hr):
    # 200 read, 99 valid: 49.5% aborts, and the text must not read as 50%.
    cards = [_card(n) for n in range(1, 100)] + [
        _card(n, title="") for n in range(100, 201)
    ]
    hr.provider.browse_pages = [_releases_page(*cards)]

    assert hr.run("releases", "--write") == 1
    assert hr.session.posts == []
    assert "only 99 of 200 cards pass (49.5%), under the minimum of 50%" in hr.stdout


# --- Backoff with jitter ------------------------------------------------------


def _always_fails():
    raise requests.Timeout("t")


def test_provider_backoff_is_5_attempts_with_bases_2_4_8_16():
    calls = []
    sleeps = []

    def fn():
        calls.append(1)
        raise requests.Timeout("t")

    with pytest.raises(collector.ProviderFailed):
        collector.call_provider(
            fn,
            sleep=sleeps.append,
            uniform=_full,
            breaker=collector.ProviderBreaker(),
        )

    assert len(calls) == 5
    assert sleeps == [2.0, 4.0, 8.0, 16.0]
    assert collector.PROVIDER_ATTEMPTS == 5
    assert collector.PROVIDER_BASE_DELAY == 2.0
    assert collector.SECOND_PASS_WAIT == 60
    assert collector.EXIT_PARTIAL == 3


def test_provider_waits_are_between_half_and_full_base_with_the_real_source():
    sleeps = []

    with pytest.raises(collector.ProviderFailed):
        collector.call_provider(
            _always_fails,
            sleep=sleeps.append,
            uniform=random.uniform,
            breaker=collector.ProviderBreaker(),
        )

    assert len(sleeps) == len(_BASES)
    for wait, base in zip(sleeps, _BASES, strict=True):
        assert base / 2 <= wait <= base


def test_injected_jitter_source_fixes_the_exact_waits():
    asked = []
    sleeps = []

    def lowest(lo, hi):
        asked.append((lo, hi))
        return lo

    with pytest.raises(collector.ProviderFailed):
        collector.call_provider(
            _always_fails,
            sleep=sleeps.append,
            uniform=lowest,
            breaker=collector.ProviderBreaker(),
        )

    assert asked == [(b / 2, b) for b in _BASES]
    assert sleeps == [b / 2 for b in _BASES]


def test_fetch_and_send_never_use_the_jitter_source(h):
    def forbidden(lo, hi):
        raise AssertionError("the jitter source is for the external provider only")

    h.uniform = forbidden
    h.session.routes[_OVERRIDES_URL] = requests.ConnectionError("x")

    assert h.run("genres") == 1
    assert h.sleeps == [1.0, 2.0]

    h.sleeps.clear()
    h.session.routes[_OVERRIDES_URL] = FakeResponse(200, {})
    h.provider = _two_songs(h).provider
    h.session.post_script = [
        FakeResponse(502),
        FakeResponse(
            200, {"ok": True, "data": {"genres": 1, "playlists": 1, "tracks": 2}}
        ),
    ]

    assert h.run("genres", "--write") == 0
    assert h.sleeps == [5]


def test_the_playlist_read_waits_with_the_jitter(h):
    h.provider.pages["PL1"] = requests.ConnectionError("down")
    h.uniform = _half

    assert h.run("genres") == 1
    assert h.sleeps == [b / 2 for b in _BASES]


def test_search_and_watch_wait_with_the_jitter():
    searching = FakeProvider()
    searching.search_errors = [requests.Timeout("t")] * collector.PROVIDER_ATTEMPTS
    search_sleeps = []
    watching = FakeProvider()
    watching.watch["audOV"] = requests.ConnectionError("down")
    watch_sleeps = []

    _resolver(searching, sleeps=search_sleeps, uniform=_half).resolve(_video(1))
    _resolver(
        watching, overrides={"vid1": "audOV"}, sleeps=watch_sleeps, uniform=_half
    ).resolve(_video(1))

    assert search_sleeps == [b / 2 for b in _BASES]
    assert watch_sleeps == [b / 2 for b in _BASES]


def test_the_releases_page_waits_with_the_jitter(hr):
    hr.provider.browse_pages = [
        requests.ConnectionError("down"),
        _releases_page(_card(1)),
    ]
    hr.uniform = _half

    assert hr.run("releases") == 0
    assert hr.sleeps == [_BASES[0] / 2]


# --- The second pass ------------------------------------------------------------


def _fails_then(answer, times=None):
    return [requests.Timeout("t")] * (times or collector.PROVIDER_ATTEMPTS) + [answer]


def test_second_pass_recovers_a_song_after_the_wait(h, caplog):
    h.provider = _provider_for(_video(1))
    h.provider.search_scripts["Song 1 (Official Video)"] = _fails_then([_audio(1)])

    with caplog.at_level(logging.INFO):
        code = h.run("genres", "--write")

    assert code == 0
    assert h.sleeps == _BASES + [collector.SECOND_PASS_WAIT]
    assert "aud1" in [t["track_id"] for t in h.session.posts[0]["json"]["tracks"]]
    [report] = h.reports()
    assert "oficial" in report.read_text()
    assert "vid1" in json.loads((h.data_dir / "cache.json").read_text())["entries"]
    assert "second pass: 1 retried, 1 recovered" in caplog.text


def test_second_pass_that_does_not_recover_keeps_the_provider_error(h, caplog):
    h.provider = _provider_for(_video(1), answers={"vid1": requests.Timeout("t")})

    with caplog.at_level(logging.INFO):
        code = h.run("genres", "--write")

    assert code == 1
    assert "error del proveedor=1" in h.stdout
    assert h.sleeps == _BASES + [collector.SECOND_PASS_WAIT] + _BASES
    assert "second pass: 1 retried, 0 recovered" in caplog.text
    assert h.session.posts == []


def test_second_pass_retries_only_the_failed_songs(h):
    h.provider = _provider_for(_video(1), _video(2))
    h.provider.search_scripts["Song 1 (Official Video)"] = _fails_then([_audio(1)])

    assert h.run("genres") == 0

    second = [c for c in h.provider.search_calls if c[0].startswith("Song 2")]
    first = [c for c in h.provider.search_calls if c[0].startswith("Song 1")]
    assert len(second) == 1
    assert len(first) == collector.PROVIDER_ATTEMPTS + 1


def test_no_second_pass_wait_when_nothing_failed(h, caplog):
    h.provider = _two_songs(h).provider

    with caplog.at_level(logging.INFO):
        assert h.run("genres", "--write") == 0

    assert h.sleeps == []
    assert collector.SECOND_PASS_WAIT not in h.sleeps
    assert "second pass" not in caplog.text


def test_a_recovered_song_is_the_first_appearance_for_repeats():
    provider = _provider_for(
        _video(1), _video(2), answers={"vid2": [_audio(1, title="Song 2")]}
    )
    provider.search_scripts["Song 1 (Official Video)"] = _fails_then([_audio(1)])
    playlists = collector.read_playlists(
        provider,
        [
            collector.GenreSource(
                "rock", (collector.SourcePlaylist("PL1", None, None, None),)
            )
        ],
        sleep=lambda s: None,
        uniform=_full,
        breaker=collector.ProviderBreaker(),
    )

    collector.resolve_all(_resolver(provider), playlists, sleep=lambda s: None)

    statuses = [r.status for r in playlists[0].resolutions]
    assert statuses == [collector.Status.OFFICIAL, collector.Status.REPEATED]


# --- The breaker -----------------------------------------------------------------


def test_provider_error_limit_is_10():
    assert collector.PROVIDER_ERROR_LIMIT == 10


def test_breaker_opens_on_the_10th_exhausted_error_and_not_before(caplog):
    limit = collector.PROVIDER_ERROR_LIMIT
    breaker = collector.ProviderBreaker()
    calls = []
    sleeps = []

    def fn():
        calls.append(1)
        raise requests.Timeout("t")

    def call():
        with pytest.raises(collector.ProviderFailed):
            collector.call_provider(
                fn, sleep=sleeps.append, uniform=_full, breaker=breaker
            )

    with caplog.at_level(logging.ERROR):
        for _ in range(limit - 1):
            call()
        assert not breaker.is_open()
        assert "provider cut off" not in caplog.text
        call()

    assert breaker.is_open()
    assert len(calls) == limit * collector.PROVIDER_ATTEMPTS
    assert caplog.text.count(f"provider cut off at error {limit} of {limit}") == 1


def test_an_open_breaker_refuses_without_calling_or_sleeping():
    breaker = _open_breaker()
    calls = []
    sleeps = []

    with pytest.raises(collector.ProviderCutOff) as raised:
        collector.call_provider(
            lambda: calls.append(1),
            sleep=sleeps.append,
            uniform=_full,
            breaker=breaker,
        )

    assert isinstance(raised.value, collector.ProviderFailed)
    assert calls == []
    assert sleeps == []
    assert breaker.errors == collector.PROVIDER_ERROR_LIMIT
    assert sum(breaker.refused.values()) == 1


def test_a_retry_in_progress_stops_when_another_thread_trips_the_cut():
    breaker = collector.ProviderBreaker()
    calls = []
    sleeps = []

    def fn():
        calls.append(1)
        for _ in range(collector.PROVIDER_ERROR_LIMIT):
            breaker.failed()
        raise requests.Timeout("t")

    with pytest.raises(collector.ProviderCutOff):
        collector.call_provider(fn, sleep=sleeps.append, uniform=_full, breaker=breaker)

    assert len(calls) == 1
    assert sleeps == []


def test_breaker_count_is_exact_under_threads(caplog):
    breaker = collector.ProviderBreaker()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(25):
            breaker.failed()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    with caplog.at_level(logging.ERROR):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert breaker.errors == 200
    assert caplog.text.count("provider cut off at error") == 1


def _rock_with_direct_audio(h, count=2):
    # A genre whose songs are audio already: it never needs a request.
    h.provider.pages["RK1"] = {
        "title": "Rock",
        "tracks": [_audio(n, videoId=f"rk{n}") for n in range(1, count + 1)],
    }


def test_cut_in_the_pool_stops_requests_skips_the_second_pass_and_sends_complete_genres(
    h, caplog, monkeypatch
):
    monkeypatch.setattr(collector, "WORKERS", 1)
    limit = collector.PROVIDER_ERROR_LIMIT
    h.set_sources([("rock", [{"source": "RK1"}]), ("pop", [{"source": "PL1"}])])
    videos = [_video(n) for n in range(1, limit + 4)]
    failing = {f"vid{n}": requests.Timeout("t") for n in range(1, limit + 1)}
    pop = _provider_for(*videos, answers=failing)
    h.provider = pop
    _rock_with_direct_audio(h)

    with caplog.at_level(logging.INFO):
        code = h.run("genres", "--write")

    assert code == 3
    assert len(pop.search_calls) == limit * collector.PROVIDER_ATTEMPTS
    assert f"error del proveedor={limit + 3}" in h.stdout
    assert collector.SECOND_PASS_WAIT not in h.sleeps
    assert "during resolution pool" in caplog.text
    assert "second pass skipped" in caplog.text
    assert "3 songs were cut off" in caplog.text
    assert "second pass:" not in caplog.text
    [post] = h.session.posts
    assert [g["slug"] for g in post["json"]["genres"]] == ["rock"]
    assert "no se envia pop:" in h.stdout
    assert h.stdout.splitlines()[-1] == "sent: genres=1 playlists=1 tracks=2"


def test_nine_errors_do_not_cut_and_the_second_pass_recovers_them(h, caplog):
    limit = collector.PROVIDER_ERROR_LIMIT
    videos = [_video(n) for n in range(1, limit)]
    h.provider = _provider_for(*videos)
    for n in range(1, limit):
        h.provider.search_scripts[f"Song {n} (Official Video)"] = _fails_then(
            [_audio(n)]
        )

    with caplog.at_level(logging.INFO):
        code = h.run("genres")

    assert code == 0
    assert collector.SECOND_PASS_WAIT in h.sleeps
    assert "provider cut off" not in caplog.text
    assert f"second pass: {limit - 1} retried, {limit - 1} recovered" in caplog.text


def test_the_cut_trips_in_the_second_pass_and_stops_it(h, caplog):
    limit = collector.PROVIDER_ERROR_LIMIT
    attempts = collector.PROVIDER_ATTEMPTS
    videos = [_video(n) for n in range(1, limit)]
    failing = {f"vid{n}": requests.Timeout("t") for n in range(1, limit)}
    h.provider = _provider_for(*videos, answers=failing)

    with caplog.at_level(logging.INFO):
        code = h.run("genres", "--write")

    assert code == 1
    assert len(h.provider.search_calls) == (limit - 1) * attempts + attempts
    assert "during second pass" in caplog.text
    assert (
        f"second pass stopped by the cut: 1 retried, 0 recovered, {limit - 2} "
        "not retried" in caplog.text
    )
    assert h.session.posts == []
    assert f"error del proveedor={limit - 1}" in h.stdout


def test_unreadable_playlists_count_toward_the_cut(h, caplog):
    limit = collector.PROVIDER_ERROR_LIMIT
    ids = [f"P{n}" for n in range(limit + 2)]
    h.set_sources(
        [("rock", [{"source": "RK1"}]), ("pop", [{"source": i} for i in ids])]
    )
    _rock_with_direct_audio(h)
    for i in ids:
        h.provider.pages[i] = requests.ConnectionError("down")

    with caplog.at_level(logging.INFO):
        code = h.run("genres", "--write")

    asked = Counter(call[0] for call in h.provider.get_playlist_calls)
    assert [asked[i] for i in ids] == [collector.PROVIDER_ATTEMPTS] * limit + [0, 0]
    assert "during source playlist reading" in caplog.text
    assert "2 source playlists and" in caplog.text
    assert code == 3
    [post] = h.session.posts
    assert [g["slug"] for g in post["json"]["genres"]] == ["rock"]
    assert f"no se envia pop: {limit + 2} source playlists not read" in h.stdout
    assert h.provider.search_calls == []
    assert collector.SECOND_PASS_WAIT not in h.sleeps


def test_an_open_breaker_still_downloads_the_input_files(h):
    h.breaker = _open_breaker()

    assert h.run("genres", "--write") == 1
    assert _SOURCES_URL in h.session.gets
    assert _OVERRIDES_URL in h.session.gets
    assert h.provider.get_playlist_calls == []
    assert h.session.posts == []


def test_releases_with_an_open_breaker_aborts_without_sending(hr, caplog):
    hr.breaker = _open_breaker()

    with caplog.at_level(logging.ERROR):
        code = hr.run("releases", "--write")

    assert code == 1
    assert hr.provider.browse_calls == []
    assert hr.sleeps == []
    assert hr.session.posts == []
    assert hr.reports() == []
    assert "aborting, nothing is sent: new releases page not read" in caplog.text


# --- Sending only the complete genres ---------------------------------------------


def _pop_with(h, cause):
    # A pop genre that is left out for one of the three reasons, and a complete
    # rock. Returns the reason line the summary must have for pop.
    h.set_sources([("pop", [{"source": "PL1"}]), ("rock", [{"source": "RK1"}])])
    _rock_with_direct_audio(h, count=1)
    if cause == "unread":
        h.provider.pages["PL1"] = requests.ConnectionError("down")
        return "1 source playlists not read"
    if cause == "provider_error":
        pop = _provider_for(_video(1), answers={"vid1": requests.Timeout("t")})
        h.provider.pages["PL1"] = pop.pages["PL1"]
        h.provider.search_answers.update(pop.search_answers)
        return "1 provider errors; 1 playlists with no saved songs"
    pop = _provider_for(_video(1), answers={"vid1": []})
    h.provider.pages["PL1"] = pop.pages["PL1"]
    h.provider.search_answers.update(pop.search_answers)
    return "1 playlists with no saved songs"


@pytest.mark.parametrize("cause", ["unread", "provider_error", "no_saved"])
def test_a_genre_left_out_sends_the_rest_and_exits_3(h, caplog, cause):
    reason = _pop_with(h, cause)

    with caplog.at_level(logging.INFO):
        code = h.run("genres", "--write")

    assert code == 3
    [post] = h.session.posts
    assert [g["slug"] for g in post["json"]["genres"]] == ["rock"]
    assert [t["track_id"] for t in post["json"]["tracks"]] == ["rk1"]
    lines = h.stdout.splitlines()
    assert f"no se envia pop: {reason}" in lines
    assert lines[-1] == "sent: genres=1 playlists=1 tracks=1"
    assert "genre not sent (pop)" in caplog.text


def test_a_partial_dry_run_exits_3_without_posting(h):
    reason = _pop_with(h, "unread")

    def refuse(*args, **kwargs):
        raise AssertionError("a dry run must not post")

    h.session.post = refuse

    assert h.run("genres") == 3
    assert f"no se envia pop: {reason}" in h.stdout.splitlines()
    assert not any(line.startswith("sent:") for line in h.stdout.splitlines())


def test_no_complete_genre_aborts_with_1_csv_and_summary(h, caplog):
    h.set_sources([("pop", [{"source": "PL1"}]), ("rock", [{"source": "PL2"}])])
    h.provider = _provider_for(_video(1), answers={"vid1": requests.Timeout("t")})
    h.provider.pages["PL2"] = {"title": "Two", "tracks": [_video(2)]}
    h.provider.search_answers["Song 2 (Official Video)"] = requests.Timeout("t")

    with caplog.at_level(logging.ERROR):
        code = h.run("genres", "--write")

    lines = h.stdout.splitlines()
    assert code == 1
    assert h.session.posts == []
    assert len(h.reports()) == 1
    assert [line.split(":")[0] for line in lines if line.startswith("no se envia")] == [
        "no se envia pop",
        "no se envia rock",
    ]
    assert (
        "aborting, nothing is sent: 2 provider errors; 2 playlists with no saved songs"
        in caplog.text
    )


def test_partial_send_that_fails_exits_1(h):
    _pop_with(h, "unread")
    h.session.post_script = [FakeResponse(400, {"ok": False, "reason": "invalid"})]

    assert h.run("genres", "--write") == 1
    assert len(h.session.posts) == 1


def test_partial_send_with_other_counts_exits_1(h):
    _pop_with(h, "unread")
    h.session.post_script = [
        FakeResponse(
            200, {"ok": True, "data": {"genres": 2, "playlists": 2, "tracks": 1}}
        )
    ]

    assert h.run("genres", "--write") == 1
    assert len(h.session.posts) == 1
    assert not any(line.startswith("sent:") for line in h.stdout.splitlines())


def test_limits_ignore_a_genre_that_is_not_sent(h):
    # The genre left out keeps MAX_TRACKS_PER_PLAYLIST + 1 saved tracks after vid1
    # ends as a provider error: over the limit, but it is never built.
    h.set_sources([("pop", [{"source": "PL1"}]), ("rock", [{"source": "RK1"}])])
    count = collector.MAX_TRACKS_PER_PLAYLIST + 2
    pop = _provider_for(*_many_videos(count), answers={"vid1": requests.Timeout("t")})
    h.provider.pages["PL1"] = pop.pages["PL1"]
    h.provider.search_answers.update(pop.search_answers)
    _rock_with_direct_audio(h, count=1)

    assert h.run("genres", "--write") == 3
    [post] = h.session.posts
    assert [g["slug"] for g in post["json"]["genres"]] == ["rock"]


def test_distinct_track_limit_counts_only_the_sent_genres(h, monkeypatch):
    monkeypatch.setattr(collector, "MAX_TRACKS", 1)
    h.set_sources([("pop", [{"source": "PL1"}]), ("rock", [{"source": "RK1"}])])
    pop = _provider_for(
        _video(1), _video(2), _video(3), answers={"vid3": requests.Timeout("t")}
    )
    h.provider.pages["PL1"] = pop.pages["PL1"]
    h.provider.search_answers.update(pop.search_answers)
    _rock_with_direct_audio(h, count=1)

    assert h.run("genres", "--write") == 3
    assert len(h.session.posts[0]["json"]["tracks"]) == 1


def test_a_limit_exceeded_in_a_sent_genre_still_exits_1(h):
    h.set_sources([("pop", [{"source": "PL1"}]), ("rock", [{"source": "RK1"}])])
    h.provider.pages["PL1"] = requests.ConnectionError("down")
    _rock_with_direct_audio(h, count=collector.MAX_TRACKS_PER_PLAYLIST + 1)

    assert h.run("genres", "--write") == 1
    assert h.session.posts == []


def test_releases_never_exit_3(hr):
    hr.provider.browse_pages = [
        _releases_page(_card(1), _card(2), _card(3), _card(4, title=""))
    ]

    assert hr.run("releases", "--write") == 0

    ctx = collector.RunContext(
        session=hr.session,
        provider=hr.provider,
        data_dir=hr.data_dir,
        out=_Out(SimpleNamespace(text="")),
        now=_NOW,
        sleep=hr.sleeps.append,
        uniform=_full,
        breaker=collector.ProviderBreaker(),
    )
    assert collector.collect_releases(ctx).skipped == ()
