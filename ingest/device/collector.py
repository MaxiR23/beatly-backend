# INFO: Collector of the ingests (genre playlists and new releases). Runs on the owner's device, not in the app: reads the provider, validates and posts the result to the back.

# One self-contained file on purpose: it is downloaded with curl onto the
# phone, so it imports only the standard library, requests and the provider
# library. Nothing from the rest of the repo (no core/, models/, pydantic,
# supabase, no Settings): the phone holds no database key, only the ingest
# token. It runs on the Python of Termux (3.11 or later), not on the 3.14 of
# the repo; pyproject.toml sets the ruff target for this folder accordingly.
#
# Usage (cron friendly, never asks anything, never reads stdin):
#
#     python collector.py genres|releases [--write]
#
# Without --write nothing is sent to the back (dry run). Exit codes: 0 the run
# is complete, 1 any failure, 2 bad arguments, 3 (genres) some genres were left
# out and the rest was sent, or would be on a dry run. The summary goes to
# stdout, the log and the CSV to ~/.beatly-collector/. See docs/ingest.md.
#
# A kind of ingest is one entry in KINDS: its collect() function and its
# endpoint in the back. What is shared: the config, the retries, the abort,
# the sending and the exit codes.

from __future__ import annotations

import argparse
import csv
import functools
import importlib.metadata
import ipaddress
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

import requests
from ytmusicapi import YTMusic as Provider
from ytmusicapi.exceptions import YTMusicServerError as ProviderServerError

logger = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

RAW_BASE_URL = (
    "https://raw.githubusercontent.com/MaxiR23/beatly-backend/main/ingest/device"
)
ENV_FILE = Path("~/.beatly-collector.env").expanduser()
DATA_DIR = Path("~/.beatly-collector").expanduser()

CONFIG_KEYS = ("BEATLY_API_URL", "BEATLY_INGEST_TOKEN")

# One pool for the whole run; the source playlists are read one by one before it.
WORKERS = 6
# Provider calls: 5 attempts, waits of 2, 4, 8 and 16 s, each one a random value
# between its half and itself (jitter). The timeouts are the (connect, read)
# ones the back uses for the same provider.
PROVIDER_ATTEMPTS = 5
PROVIDER_BASE_DELAY = 2.0
PROVIDER_TIMEOUT = (3.05, 10)
# Provider calls that exhaust their retries in a run before the collector stops
# asking the external provider.
PROVIDER_ERROR_LIMIT = 10
# Seconds to wait before the second pass over the songs that ended as provider
# errors.
SECOND_PASS_WAIT = 60
# Reading the versioned input files from the public repo: 3 attempts, waits of
# 1 s and 2 s, no jitter.
FETCH_TIMEOUT = (10, 30)
FETCH_ATTEMPTS = 3
FETCH_BASE_DELAY = 1.0
# Sending to the back: 4 attempts, waits of 5, 15 and 45 s. The read timeout is
# above the largest INGEST_RPC_TIMEOUT (300 s) so the back answers first.
SEND_DELAYS = (5, 15, 45)
SEND_TIMEOUT = (10, 330)
SEND_RETRY_STATUSES = (502, 503, 504)

# Exit code of a genres run that left some genres out and sent the rest.
EXIT_PARTIAL = 3

SEARCH_FILTER = "songs"
SEARCH_LIMIT = 5
SEARCH_ARTISTS = 2

# Bounds the back enforces (models/ingest.py). An item over any of them is
# "datos incompletos" here, so one odd song does not get the whole run rejected.
# The limits of a whole submission (the four below the ids) and the id and slug
# patterns are checked before sending, so a dry run predicts what --write
# gets. They are copies, not imports: a test compares them with the back's.
MAX_TITLE = 300
MAX_DURATION = 86_400
MAX_ARTISTS = 20
MAX_URL = 2048
MAX_PLAYLIST_TITLE = 200
MAX_DESCRIPTION = 2000
MAX_CATEGORY = 100
MAX_ID = 128
MAX_SLUG = 100
MAX_GENRES = 50
MAX_PLAYLISTS_PER_GENRE = 50
MAX_TRACKS_PER_PLAYLIST = 500
MAX_TRACKS = 20_000
ID_PATTERN = r"[A-Za-z0-9_-]+"
SLUG_PATTERN = r"[^\s,()\"'\\]+"
_ID = re.compile(ID_PATTERN)
_SLUG = re.compile(SLUG_PATTERN)

# New releases (kind "releases"). Same rule as the block above: copies of the
# back's limits and patterns, compared by a test. MAX_TITLE, MAX_URL,
# MAX_ARTISTS and MAX_ID are reused.
MAX_RELEASES = 500
ALBUM_ID_PATTERN = r"MPREb_[A-Za-z0-9_-]+"
AUDIO_PLAYLIST_PATTERN = r"OLAK5uy_[A-Za-z0-9_-]+"
ARTIST_ID_PATTERN = r"UC[A-Za-z0-9_-]{22}"
_ALBUM_ID = re.compile(ALBUM_ID_PATTERN)
_AUDIO_PLAYLIST = re.compile(AUDIO_PLAYLIST_PATTERN)
_ARTIST_ID = re.compile(ARTIST_ID_PATTERN)
# The text of the first run of the subtitle, and the value that is stored.
RELEASE_TYPES = {"Album": "album", "Single": "single", "EP": "ep"}
FORBIDDEN_ARTIST_NAMES = frozenset({"null", "none", "undefined"})
# The exact texts of the runs that separate the artists in the subtitle.
RELEASE_SEPARATORS = frozenset({" • ", " & ", ", "})
# The language is fixed; the location is not: the list depends on the IP on
# purpose.
RELEASES_LANGUAGE = "en"
NEW_RELEASES_BROWSE_ID = "FEmusic_new_releases_albums"
ARTIST_PAGE_TYPE = "MUSIC_PAGE_TYPE_ARTIST"
# Fewer cards that pass than this share of the cards read means the format of
# the page changed (the two measured samples kept 95%): the run aborts and the
# app keeps showing the last good list. Exactly the share does not abort. Only
# the collector knows this rule; the back receives the cards that pass.
MIN_KEPT_RATIO = 0.5

AUDIO_VIDEO_TYPE = "MUSIC_VIDEO_TYPE_ATV"


class Status(StrEnum):
    OFFICIAL = "oficial"
    ALREADY_AUDIO = "ya era audio"
    NAME_ONLY = "solo por nombre"
    NO_MATCH = "sin match"
    OVERRIDE = "excepcion manual"
    INCOMPLETE = "datos incompletos"
    REPEATED = "repetida"
    PROVIDER_ERROR = "error del proveedor"


# The statuses that are sent to the back.
SAVED = frozenset({Status.OFFICIAL, Status.ALREADY_AUDIO, Status.OVERRIDE})


# --- Errors ------------------------------------------------------------------


class CollectorError(Exception):
    pass


class ConfigError(CollectorError):
    # The message is the collector's own and carries no data from the input.
    pass


class ProviderFailed(CollectorError):
    pass


class ProviderCutOff(ProviderFailed):
    # The breaker is open: the call was refused without a request.
    pass


class FetchFailed(CollectorError):
    pass


class SendFailed(CollectorError):
    pass


# --- Config ------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    api_url: str
    token: str = field(repr=False)


_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _http_allowed(host: str) -> bool:
    # http is for the private network and the machine itself, to try the back
    # before deploying it. No name is resolved: a host name other than
    # localhost never qualifies.
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.version == 4 and any(address in net for net in _PRIVATE_NETWORKS)


def validate_api_url(url: str) -> str:
    # https is required; http only to localhost and the private ranges
    # 10.0.0.0/8, 172.16.0.0/12 and 192.168.0.0/16. The messages never repeat
    # the value: a URL can carry credentials.
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ConfigError("BEATLY_API_URL is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ConfigError("BEATLY_API_URL must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("BEATLY_API_URL must not carry credentials")
    if parts.scheme == "http" and not _http_allowed(host):
        raise ConfigError(
            "BEATLY_API_URL must use https (http only for localhost "
            "and the private network)"
        )
    return url.rstrip("/")


def read_config(path: Path) -> Config:
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        raise ConfigError("the config file does not exist") from None
    if mode & 0o077:
        raise ConfigError("the config file must have permissions 600")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in CONFIG_KEYS:
            raise ConfigError("the config file has a line that is not a known key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    missing = [key for key in CONFIG_KEYS if not values.get(key)]
    if missing:
        raise ConfigError(f"the config file lacks {', '.join(missing)}")
    return Config(
        api_url=validate_api_url(values["BEATLY_API_URL"]),
        token=values["BEATLY_INGEST_TOKEN"],
    )


# --- Input files -------------------------------------------------------------


@dataclass(frozen=True)
class SourcePlaylist:
    source: str
    title: str | None
    description: str | None
    category: str | None


@dataclass(frozen=True)
class GenreSource:
    slug: str
    playlists: tuple[SourcePlaylist, ...]


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{what} must be a non-empty string")
    return value


def _valid_id(value: str) -> bool:
    return len(value) <= MAX_ID and _ID.fullmatch(value) is not None


def parse_genre_sources(data: Any) -> list[GenreSource]:
    if not isinstance(data, dict) or set(data) != {"genres"}:
        raise ConfigError("genre sources must be an object with only 'genres'")
    if not isinstance(data["genres"], list):
        raise ConfigError("'genres' must be a list")
    if len(data["genres"]) > MAX_GENRES:
        raise ConfigError(f"more than {MAX_GENRES} genres (the back's limit)")
    genres: list[GenreSource] = []
    slugs: set[str] = set()
    for genre in data["genres"]:
        if not isinstance(genre, dict) or set(genre) != {"slug", "playlists"}:
            raise ConfigError("a genre must have exactly 'slug' and 'playlists'")
        slug = _require_text(genre["slug"], "a genre slug")
        if len(slug) > MAX_SLUG or not _SLUG.fullmatch(slug):
            raise ConfigError(
                f"genre {slug!r}: the slug does not match the back's pattern"
            )
        if slug in slugs:
            raise ConfigError("a genre slug is repeated")
        slugs.add(slug)
        if not isinstance(genre["playlists"], list) or not genre["playlists"]:
            raise ConfigError("a genre needs at least one playlist")
        if len(genre["playlists"]) > MAX_PLAYLISTS_PER_GENRE:
            raise ConfigError(
                f"genre {slug!r}: more than {MAX_PLAYLISTS_PER_GENRE} playlists "
                "(the back's limit)"
            )
        playlists: list[SourcePlaylist] = []
        sources: set[str] = set()
        for entry in genre["playlists"]:
            allowed = {"source", "title", "description", "category"}
            if (
                not isinstance(entry, dict)
                or "source" not in entry
                or set(entry) - allowed
            ):
                raise ConfigError("a playlist needs 'source' and only known keys")
            source = _require_text(entry["source"], "a playlist source")
            if not _valid_id(source):
                raise ConfigError(
                    f"genre {slug!r}: playlist {source!r} does not match the "
                    "back's id pattern"
                )
            if source in sources:
                raise ConfigError("a playlist source is repeated in a genre")
            sources.add(source)
            optional = {
                key: _require_text(entry[key], f"a playlist {key}")
                for key in ("title", "description", "category")
                if key in entry
            }
            if len(optional.get("category", "")) > MAX_CATEGORY:
                raise ConfigError("a playlist category is too long")
            playlists.append(
                SourcePlaylist(
                    source=source,
                    title=optional.get("title"),
                    description=optional.get("description"),
                    category=optional.get("category"),
                )
            )
        genres.append(GenreSource(slug=slug, playlists=tuple(playlists)))
    return genres


def parse_overrides(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ConfigError("overrides must be an object")
    overrides: dict[str, str] = {}
    for video_id, audio_id in data.items():
        _require_text(video_id, "an override video id")
        overrides[video_id] = _require_text(audio_id, "an override audio id")
    return overrides


# --- Retries and fetching ----------------------------------------------------

PROVIDER_RETRYABLE: tuple[type[BaseException], ...] = (
    requests.RequestException,
    ProviderServerError,
    ValueError,
    IndexError,
    KeyError,
    TypeError,
)


class ProviderBreaker:
    # One per run, shared by every thread that calls the external provider.
    # After PROVIDER_ERROR_LIMIT calls exhaust their retries it opens and
    # every later attempt is refused without a request.
    def __init__(self, limit: int = PROVIDER_ERROR_LIMIT) -> None:
        self._lock = threading.Lock()
        self._limit = limit
        self._stage = "start"
        self.errors = 0
        self.refused: dict[str, int] = {}

    def enter(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def is_open(self) -> bool:
        with self._lock:
            return self.errors >= self._limit

    def failed(self) -> None:
        with self._lock:
            self.errors += 1
            if self.errors == self._limit:
                logger.error(
                    "provider cut off at error %d of %d, during %s: "
                    "no new requests to the external provider",
                    self.errors,
                    self._limit,
                    self._stage,
                )

    def refuse(self) -> None:
        with self._lock:
            self.refused[self._stage] = self.refused.get(self._stage, 0) + 1


def call_with_retries(
    fn: Callable[[], Any],
    *,
    attempts: int,
    base_delay: float,
    retry_on: tuple[type[BaseException], ...] = PROVIDER_RETRYABLE,
    failure: type[CollectorError] = ProviderFailed,
    sleep: Callable[[float], None] = time.sleep,
    uniform: Callable[[float, float], float] | None = None,
) -> Any:
    # Waits base_delay * 2 ** (n - 1) after attempt n; with a uniform source
    # the wait is a value between half of that and all of it (jitter). The
    # failure raised at the end is chained to the last error, but its text is
    # the collector's.
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if attempt < attempts:
                delay = base_delay * 2 ** (attempt - 1)
                sleep(uniform(delay / 2, delay) if uniform is not None else delay)
    raise failure("retries exhausted") from last


def call_provider(
    fn: Callable[[], Any],
    *,
    sleep: Callable[[float], None],
    uniform: Callable[[float, float], float],
    breaker: ProviderBreaker,
) -> Any:
    # Every call to the external provider goes through here. The breaker is
    # checked before each attempt and each wait, so retries in progress in
    # other threads stop too.
    def attempt() -> Any:
        if breaker.is_open():
            breaker.refuse()
            raise ProviderCutOff("the external provider is cut off")
        return fn()

    def wait(seconds: float) -> None:
        if not breaker.is_open():
            sleep(seconds)

    try:
        return call_with_retries(
            attempt,
            attempts=PROVIDER_ATTEMPTS,
            base_delay=PROVIDER_BASE_DELAY,
            sleep=wait,
            uniform=uniform,
        )
    except ProviderCutOff:
        raise
    except ProviderFailed:
        breaker.failed()
        raise


def fetch_json(
    session: Any,
    url: str,
    *,
    missing_ok: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    # A 404 is "the file is not there" only when missing_ok; any other
    # status above 200 is a failure, and 5xx and network errors are retried.
    def get() -> Any:
        response = session.get(url, timeout=FETCH_TIMEOUT)
        if response.status_code >= 500:
            raise requests.HTTPError("server error")
        return response

    response = call_with_retries(
        get,
        attempts=FETCH_ATTEMPTS,
        base_delay=FETCH_BASE_DELAY,
        retry_on=(requests.RequestException,),
        failure=FetchFailed,
        sleep=sleep,
    )
    if response.status_code == 404 and missing_ok:
        return None
    if response.status_code != 200:
        raise FetchFailed("unexpected status")
    # Outside the retried call: an invalid JSON is the file's problem.
    try:
        return response.json()
    except ValueError:
        raise ConfigError("a downloaded input file is not valid JSON") from None


# --- Titles ------------------------------------------------------------------

_FEAT_GROUP = re.compile(r"[(\[]\s*(?:ft|feat)\b\.?[^)\]]*[)\]]")
_FEAT_LOOSE = re.compile(r"\b(?:ft|feat)\b.*")
_NOISE_WORDS = re.compile(
    r"\b(?:official|oficial|video|lyrics?|visualizer|audio|live|en vivo)\b"
)
_GROUP = re.compile(r"[(\[][^)\]]*[)\]]")
_NOT_ALNUM = re.compile(r"[\W_]+")


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _FEAT_GROUP.sub(" ", text)
    text = _FEAT_LOOSE.sub("", text)
    text = _GROUP.sub(
        lambda m: " " if _NOISE_WORDS.search(m.group()) else m.group(), text
    )
    # A suffix " - ..." that holds one of the words goes too.
    start = text.find(" - ")
    while start != -1:
        if _NOISE_WORDS.search(text[start:]):
            text = text[:start]
            break
        start = text.find(" - ", start + 1)
    return _NOT_ALNUM.sub(" ", text).strip()


# --- Songs -------------------------------------------------------------------


@dataclass(frozen=True)
class Track:
    track_id: str
    title: str
    artists: tuple[tuple[str | None, str], ...]
    album: str
    album_id: str
    duration_seconds: int
    thumbnail_url: str

    def to_row(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "title": self.title,
            "artists": [{"id": i, "name": n} for i, n in self.artists],
            "album": self.album,
            "album_id": self.album_id,
            "duration_seconds": self.duration_seconds,
            "thumbnail_url": self.thumbnail_url,
        }

    @classmethod
    def from_row(cls, row: Any) -> Track:
        return cls(
            track_id=row["track_id"],
            title=row["title"],
            artists=tuple((a["id"], a["name"]) for a in row["artists"]),
            album=row["album"],
            album_id=row["album_id"],
            duration_seconds=row["duration_seconds"],
            thumbnail_url=row["thumbnail_url"],
        )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def duration_seconds(item: dict) -> int | None:
    value = item.get("duration_seconds")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    for key in ("length", "duration"):
        text = item.get(key)
        if isinstance(text, str) and re.fullmatch(r"\d+(:\d{1,2}){1,2}", text):
            total = 0
            for part in text.split(":"):
                total = total * 60 + int(part)
            return total or None
    return None


def _cover(item: dict) -> str:
    thumbnails = item.get("thumbnails") or item.get("thumbnail")
    if not isinstance(thumbnails, list):
        return ""
    urls = [_text(t.get("url")) for t in thumbnails if isinstance(t, dict)]
    urls = [u for u in urls if u]
    return urls[-1] if urls else ""


def track_row(item: Any) -> Track | None:
    # None means the item cannot make a valid row of tracks ("datos
    # incompletos"). The rule of the first artist (it has an id) is the back's,
    # copied here because this file imports nothing from it.
    if not isinstance(item, dict):
        return None
    video_id = _text(item.get("videoId"))
    title = _text(item.get("title"))
    artists_raw = item.get("artists")
    album_raw = item.get("album")
    if not _valid_id(video_id) or not title or len(title) > MAX_TITLE:
        return None
    if not isinstance(artists_raw, list) or not artists_raw:
        return None
    artists: list[tuple[str | None, str]] = []
    for position, artist in enumerate(artists_raw):
        if not isinstance(artist, dict):
            return None
        artist_id = _text(artist.get("id")) or None
        name = _text(artist.get("name"))
        if position == 0 and (artist_id is None or not name):
            return None
        if name:
            artists.append((artist_id, name))
    if len(artists) > MAX_ARTISTS:
        return None
    if not isinstance(album_raw, dict):
        return None
    album = _text(album_raw.get("name"))
    album_id = _text(album_raw.get("id"))
    if not album or len(album) > MAX_TITLE or not _valid_id(album_id):
        return None
    duration = duration_seconds(item)
    cover = _cover(item)
    if duration is None or duration > MAX_DURATION:
        return None
    if not cover.startswith("https://") or len(cover) > MAX_URL:
        return None
    return Track(
        track_id=video_id,
        title=title,
        artists=tuple(artists),
        album=album,
        album_id=album_id,
        duration_seconds=duration,
        thumbnail_url=cover,
    )


def _artist_ids(item: dict) -> set[str]:
    artists = item.get("artists")
    if not isinstance(artists, list):
        return set()
    return {
        _text(a.get("id"))
        for a in artists
        if isinstance(a, dict) and _text(a.get("id"))
    }


def _artist_names(item: dict) -> list[str]:
    artists = item.get("artists")
    if not isinstance(artists, list):
        return []
    return [_text(a.get("name")) for a in artists if isinstance(a, dict)]


def title_matches(result_title: str, video_title: str) -> bool:
    result, video = normalize_title(result_title), normalize_title(video_title)
    return bool(result) and (result == video or result in video)


@dataclass(frozen=True)
class Resolution:
    video_id: str
    title: str
    artists: str
    status: Status
    audio_id: str | None = None
    track: Track | None = None
    duration_diff: int | None = None


class Resolver:
    def __init__(
        self,
        provider: Any,
        *,
        overrides: dict[str, str],
        cache: dict[str, Track],
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        breaker: ProviderBreaker | None = None,
    ) -> None:
        self._provider = provider
        self._overrides = overrides
        self._cache = cache
        self._sleep = sleep
        self._uniform = uniform
        # Public: resolve_all reads it. Never a default shared by the module.
        self.breaker = breaker if breaker is not None else ProviderBreaker()

    def _call(self, fn: Callable[[], Any]) -> Any:
        return call_provider(
            fn, sleep=self._sleep, uniform=self._uniform, breaker=self.breaker
        )

    def resolve(self, item: dict) -> Resolution:
        video_id = _text(item.get("videoId"))
        title = _text(item.get("title"))
        artists = ", ".join(n for n in _artist_names(item) if n)

        def done(status: Status, track: Track | None = None) -> Resolution:
            video_duration = duration_seconds(item)
            diff = (
                track.duration_seconds - video_duration
                if track is not None and video_duration is not None
                else None
            )
            return Resolution(
                video_id=video_id,
                title=title,
                artists=artists,
                status=status,
                audio_id=track.track_id if track is not None else None,
                track=track,
                duration_diff=diff,
            )

        try:
            if video_id in self._overrides:
                return self._override(item, self._overrides[video_id], done)
            if not video_id or item.get("isAvailable") is False:
                return done(Status.INCOMPLETE)
            if item.get("videoType") == AUDIO_VIDEO_TYPE:
                track = track_row(item)
                return done(Status.ALREADY_AUDIO if track else Status.INCOMPLETE, track)
            if video_id in self._cache:
                return done(Status.OFFICIAL, self._cache[video_id])
            return self._search(item, title, done)
        except ProviderCutOff:
            return done(Status.PROVIDER_ERROR)
        except ProviderFailed as exc:
            logger.warning("provider failed after retries: %s", _cause(exc))
            return done(Status.PROVIDER_ERROR)

    def _override(self, item: dict, audio_id: str, done: Callable[..., Resolution]):
        # The audio the file names is read with its watch playlist; its first
        # item must be the requested id, or the file is wrong.
        first = self._call(
            lambda: self._provider.get_watch_playlist(videoId=audio_id, limit=1)[
                "tracks"
            ][0]
        )
        if not isinstance(first, dict) or first.get("videoId") != audio_id:
            raise ConfigError("an override audio id is not the one the provider gave")
        track = track_row(first)
        return done(Status.OVERRIDE if track else Status.INCOMPLETE, track)

    def _search(self, item: dict, title: str, done: Callable[..., Resolution]):
        names = [n for n in _artist_names(item) if n][:SEARCH_ARTISTS]
        query = " ".join([title, *names])
        results = self._call(
            lambda: self._provider.search(
                query, filter=SEARCH_FILTER, limit=SEARCH_LIMIT
            )
        )
        wanted = _artist_ids(item)
        name_only = False
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, dict) or not title_matches(
                _text(result.get("title")), title
            ):
                continue
            if wanted & _artist_ids(result):
                track = track_row(result)
                return done(Status.OFFICIAL if track else Status.INCOMPLETE, track)
            name_only = True
        return done(Status.NAME_ONLY if name_only else Status.NO_MATCH)


def _cause(exc: BaseException) -> str:
    # Only the type: a provider error can carry URLs with ids.
    return type(exc.__cause__ or exc).__name__


@dataclass
class PlaylistResult:
    genre: str
    source: SourcePlaylist
    read: bool
    title: str = ""
    description: str | None = None
    items: list[dict] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)

    def saved(self) -> list[Resolution]:
        return [r for r in self.resolutions if r.status in SAVED]


def mark_repeats(resolutions: list[Resolution]) -> list[Resolution]:
    # The first appearance of an audio keeps its status; the rest are repeats.
    seen: set[str] = set()
    out: list[Resolution] = []
    for resolution in resolutions:
        if resolution.status in SAVED and resolution.audio_id is not None:
            if resolution.audio_id in seen:
                resolution = Resolution(
                    **{**resolution.__dict__, "status": Status.REPEATED}
                )
            seen.add(resolution.audio_id)
        out.append(resolution)
    return out


def read_playlists(
    provider: Any,
    genres: Sequence[GenreSource],
    *,
    sleep: Callable[[float], None],
    uniform: Callable[[float, float], float],
    breaker: ProviderBreaker,
) -> list[PlaylistResult]:
    # One by one, before the pool. A playlist that cannot be read after the
    # retries is marked as not read and the run goes on (it aborts later).
    breaker.enter("source playlist reading")
    results: list[PlaylistResult] = []
    for genre in genres:
        for source in genre.playlists:
            try:
                page = call_provider(
                    lambda s=source: provider.get_playlist(s.source, limit=None),
                    sleep=sleep,
                    uniform=uniform,
                    breaker=breaker,
                )
                tracks = page["tracks"]
                if not isinstance(tracks, list):
                    raise TypeError("tracks is not a list")
            except ProviderCutOff:
                results.append(PlaylistResult(genre.slug, source, read=False))
                continue
            except (ProviderFailed, TypeError, KeyError) as exc:
                logger.warning(
                    "source playlist not read (%s): %s", genre.slug, _cause(exc)
                )
                results.append(PlaylistResult(genre.slug, source, read=False))
                continue
            results.append(
                PlaylistResult(
                    genre=genre.slug,
                    source=source,
                    read=True,
                    title=source.title or _text(page.get("title")),
                    description=source.description
                    or (_text(page.get("description")) or None),
                    items=[i for i in tracks if isinstance(i, dict)],
                )
            )
    return results


def resolve_all(
    resolver: Resolver,
    playlists: Sequence[PlaylistResult],
    *,
    sleep: Callable[[float], None],
) -> list[Resolution]:
    # A single pool for every item of every playlist, in source order, then a
    # second pass, one by one, over the songs that ended as provider errors
    # (skipped when the breaker is open). Returns every resolution of the run,
    # in source order.
    breaker = resolver.breaker
    breaker.enter("resolution pool")
    tasks = [(p, item) for p in playlists if p.read for item in p.items]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(resolver.resolve, item) for _, item in tasks]
        try:
            resolved = [future.result() for future in futures]
        finally:
            for future in futures:
                future.cancel()
    failed = [n for n, r in enumerate(resolved) if r.status is Status.PROVIDER_ERROR]
    if failed and breaker.is_open():
        logger.warning(
            "second pass skipped, the external provider is cut off: %d songs stay "
            "as provider errors",
            len(failed),
        )
    elif failed:
        # One by one after a pause, with the same retries: a burst of bad
        # answers is usually over by then. It stops if the cut trips here.
        breaker.enter("second pass")
        sleep(SECOND_PASS_WAIT)
        retried = 0
        for n in failed:
            if breaker.is_open():
                break
            resolved[n] = resolver.resolve(tasks[n][1])
            retried += 1
        recovered = sum(
            1 for n in failed if resolved[n].status is not Status.PROVIDER_ERROR
        )
        if retried < len(failed):
            logger.warning(
                "second pass stopped by the cut: %d retried, %d recovered, "
                "%d not retried",
                retried,
                recovered,
                len(failed) - retried,
            )
        else:
            logger.info("second pass: %d retried, %d recovered", retried, recovered)
    for (playlist, _), resolution in zip(tasks, resolved, strict=True):
        playlist.resolutions.append(resolution)
    for playlist in playlists:
        playlist.resolutions = mark_repeats(playlist.resolutions)
    return resolved


# --- Cache -------------------------------------------------------------------

CACHE_VERSION = 1


def load_cache(path: Path) -> dict[str, Track]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["version"] != CACHE_VERSION:
            raise ValueError
        entries = {vid: Track.from_row(row) for vid, row in data["entries"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ConfigError("the cache file is unreadable: delete it") from None
    return entries


def save_cache(path: Path, entries: dict[str, Track]) -> None:
    # Atomic: a temporary file in the same folder, then a replace.
    payload = {
        "version": CACHE_VERSION,
        "entries": {vid: track.to_row() for vid, track in sorted(entries.items())},
    }
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".cache-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


# --- Report ------------------------------------------------------------------

CSV_HEADER = (
    "genero",
    "playlist",
    "titulo",
    "artistas",
    "id_video",
    "id_audio",
    "estado",
    "diferencia_duracion",
)


def _csv_safe(value: str) -> str:
    # A cell that a spreadsheet would read as a formula gets a quote in front.
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def write_csv(
    reports_dir: Path, now: datetime, playlists: Iterable[PlaylistResult]
) -> Path:
    reports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = reports_dir / f"genre-ingest-{now.strftime('%Y%m%d-%H%M%S')}.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_HEADER)
        for playlist in playlists:
            for r in playlist.resolutions:
                writer.writerow(
                    [
                        _csv_safe(playlist.genre),
                        _csv_safe(playlist.source.source),
                        _csv_safe(r.title),
                        _csv_safe(r.artists),
                        _csv_safe(r.video_id),
                        _csv_safe(r.audio_id or ""),
                        r.status.value,
                        "" if r.duration_diff is None else r.duration_diff,
                    ]
                )
    return path


def _counters(resolutions: Iterable[Resolution]) -> str:
    counts = {status: 0 for status in Status}
    saved = 0
    for r in resolutions:
        counts[r.status] += 1
        saved += r.status in SAVED
    parts = [f"{status.value}={counts[status]}" for status in Status]
    return " ".join([*parts, f"se guardan={saved}"])


def write_summary(out: TextIO, playlists: Sequence[PlaylistResult]) -> None:
    for playlist in playlists:
        label = f"{playlist.genre} {playlist.source.source}"
        if not playlist.read:
            out.write(f"{label}: no leida\n")
        else:
            out.write(f"{label}: {_counters(playlist.resolutions)}\n")
    everything = [r for p in playlists for r in p.resolutions]
    out.write(f"total: {_counters(everything)}\n")


# --- Payload and sending -----------------------------------------------------


def build_genre_payload(playlists: Sequence[PlaylistResult]) -> dict[str, Any]:
    # The shape of POST /ingest/genres: playlists reference tracks by id and
    # the rows of tracks go once, the first appearance winning.
    genres: dict[str, list[dict[str, Any]]] = {}
    rows: dict[str, Track] = {}
    for playlist in playlists:
        title = (playlist.title or "")[:MAX_PLAYLIST_TITLE].strip()
        if not title:
            raise ConfigError("a source playlist has no title: set one in the file")
        saved = playlist.saved()
        if len(saved) > MAX_TRACKS_PER_PLAYLIST:
            raise ConfigError(
                f"genre {playlist.genre!r}, playlist {playlist.source.source!r}: "
                f"{len(saved)} tracks, over the back's limit of "
                f"{MAX_TRACKS_PER_PLAYLIST}"
            )
        for r in saved:
            rows.setdefault(r.audio_id, r.track)
        genres.setdefault(playlist.genre, []).append(
            {
                "source_playlist_id": playlist.source.source,
                "title": title,
                "description": (playlist.description or None)
                and playlist.description[:MAX_DESCRIPTION],
                "category": playlist.source.category,
                "tracks": [r.audio_id for r in saved],
            }
        )
    if len(rows) > MAX_TRACKS:
        raise ConfigError(
            f"{len(rows)} distinct tracks, over the back's limit of {MAX_TRACKS}"
        )
    return {
        "genres": [
            {"slug": slug, "playlists": lists} for slug, lists in genres.items()
        ],
        "tracks": [track.to_row() for track in rows.values()],
    }


def genre_counts(payload: dict[str, Any]) -> dict[str, int]:
    playlists = [p for g in payload["genres"] for p in g["playlists"]]
    return {
        "genres": len(payload["genres"]),
        "playlists": len(playlists),
        "tracks": sum(len(p["tracks"]) for p in playlists),
    }


def _reason(response: Any) -> str:
    # The stable identifier of the error body, or "unknown": never free text.
    try:
        reason = response.json().get("reason")
    except (ValueError, AttributeError):
        return "unknown"
    return (
        reason
        if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason)
        else "unknown"
    )


def send(
    session: Any,
    config: Config,
    path: str,
    payload: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    # Safe to repeat: the endpoint leaves the same rows for the same body.
    # Every requests error (the network ones) and 502/503/504 are retried; a 4xx or a 200 never.
    url = config.api_url + path
    headers = {"Authorization": f"Bearer {config.token}"}
    attempts = len(SEND_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            response = session.post(
                url, json=payload, headers=headers, timeout=SEND_TIMEOUT
            )
        except requests.RequestException as exc:
            # Only the type goes to the log: the message can carry the URL.
            logger.warning(
                "send attempt %d/%d failed: %s", attempt, attempts, type(exc).__name__
            )
        else:
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()["data"]
                except (ValueError, KeyError, TypeError):
                    raise SendFailed("the answer is not the expected shape") from None
                if not isinstance(data, dict):
                    raise SendFailed("the answer is not the expected shape")
                return data
            retryable = status in SEND_RETRY_STATUSES
            logger.log(
                logging.WARNING if retryable else logging.ERROR,
                "send rejected status=%d reason=%s",
                status,
                _reason(response),
            )
            if not retryable:
                raise SendFailed("the back rejected the submission")
        if attempt < attempts:
            sleep(SEND_DELAYS[attempt - 1])
    raise SendFailed("retries exhausted")


# --- Releases ----------------------------------------------------------------


class Discard(StrEnum):
    # In the order the rules are evaluated: a card gets the first that fails.
    UNREADABLE = "tarjeta ilegible"
    ALBUM_ID = "id de album invalido"
    AUDIO_PLAYLIST = "playlist de audio invalida"
    TYPE = "tipo no permitido"
    TITLE = "titulo invalido"
    NO_ARTISTS = "sin artistas"
    ARTIST_ID = "artista sin id valido"
    TOO_MANY_ARTISTS = "demasiados artistas"
    ARTIST_UNLINKED = "artista sin link"
    ARTIST_NAME = "nombre de artista invalido"
    COVER = "portada invalida"
    REPEATED = "repetido"


@dataclass(frozen=True)
class Release:
    album_id: str
    audio_playlist_id: str
    type: str
    title: str
    artists: tuple[tuple[str, str], ...]
    thumbnail_url: str

    def to_row(self) -> dict[str, Any]:
        return {
            "album_id": self.album_id,
            "audio_playlist_id": self.audio_playlist_id,
            "type": self.type,
            "title": self.title,
            "artists": [{"id": id_, "name": name} for id_, name in self.artists],
            "thumbnail_url": self.thumbnail_url,
        }


@dataclass(frozen=True)
class ReleaseCard:
    # What was read, for the CSV; release is set only when the card passes.
    album_id: str
    audio_playlist_id: str
    type_text: str
    title: str
    artist_names: tuple[str, ...]
    release: Release | None = None
    reason: Discard | None = None

    @property
    def saved(self) -> bool:
        return self.release is not None


def _dig(node: Any, *path: str) -> Any:
    # Follows the keys through dicts; None as soon as one is missing.
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def grid_items(page: Any) -> list | None:
    # The cards of the page, or None if the page does not have the measured
    # shape: a single section with a grid, all in this one response. A grid
    # with continuations or any other shape is an unreadable page, not a
    # partial one.
    sections = _dig(
        page,
        "contents",
        "singleColumnBrowseResultsRenderer",
        "tabs",
    )
    if not isinstance(sections, list) or not sections:
        return None
    sections = _dig(
        sections[0],
        "tabRenderer",
        "content",
        "sectionListRenderer",
        "contents",
    )
    if not isinstance(sections, list) or len(sections) != 1:
        return None
    grid = _dig(sections[0], "gridRenderer")
    if not isinstance(grid, dict) or "continuations" in grid:
        return None
    items = grid.get("items")
    return items if isinstance(items, list) else None


def _run_text(run: Any) -> str:
    text = run.get("text") if isinstance(run, dict) else None
    return text if isinstance(text, str) else ""


def parse_release_card(item: Any) -> ReleaseCard:
    # Parsed on the raw response, not with the library's parsers: they drop
    # the artist names that have no link, and a card with one of those must be
    # discarded whole (measured). The order of the checks is the order of the
    # rules; the reason is the first that fails.
    renderer = _dig(item, "musicTwoRowItemRenderer")
    if not isinstance(renderer, dict):
        return ReleaseCard("", "", "", "", (), reason=Discard.UNREADABLE)

    album_id = _text(_dig(renderer, "navigationEndpoint", "browseEndpoint", "browseId"))
    audio_playlist_id = _text(
        _dig(
            renderer,
            "thumbnailOverlay",
            "musicItemThumbnailOverlayRenderer",
            "content",
            "musicPlayButtonRenderer",
            "playNavigationEndpoint",
            "watchPlaylistEndpoint",
            "playlistId",
        )
    )
    title_runs = _dig(renderer, "title", "runs")
    title = (
        "".join(_run_text(run) for run in title_runs).strip()
        if isinstance(title_runs, list)
        else ""
    )

    subtitle = _dig(renderer, "subtitle", "runs")
    runs = subtitle if isinstance(subtitle, list) else []
    type_text = _run_text(runs[0]) if runs else ""
    names: list[str] = []
    linked: list[tuple[str, str, str]] = []  # (id, page type, name)
    unlinked = 0
    for run in runs[1:]:
        text = _run_text(run)
        if isinstance(run, dict) and isinstance(run.get("navigationEndpoint"), dict):
            endpoint = run["navigationEndpoint"]
            browse_id = _text(_dig(endpoint, "browseEndpoint", "browseId"))
            page_type = _text(
                _dig(
                    endpoint,
                    "browseEndpoint",
                    "browseEndpointContextSupportedConfigs",
                    "browseEndpointContextMusicConfig",
                    "pageType",
                )
            )
            linked.append((browse_id, page_type, text))
            names.append(text)
        elif text not in RELEASE_SEPARATORS:
            unlinked += 1
            names.append(text)

    cover = _cover(
        {
            "thumbnails": _dig(
                renderer,
                "thumbnailRenderer",
                "musicThumbnailRenderer",
                "thumbnail",
                "thumbnails",
            )
        }
    )

    def discard(reason: Discard) -> ReleaseCard:
        return ReleaseCard(
            album_id, audio_playlist_id, type_text, title, tuple(names), reason=reason
        )

    if len(album_id) > MAX_ID or not _ALBUM_ID.fullmatch(album_id):
        return discard(Discard.ALBUM_ID)
    if len(audio_playlist_id) > MAX_ID or not _AUDIO_PLAYLIST.fullmatch(
        audio_playlist_id
    ):
        return discard(Discard.AUDIO_PLAYLIST)
    if type_text not in RELEASE_TYPES:
        return discard(Discard.TYPE)
    if not title or len(title) > MAX_TITLE:
        return discard(Discard.TITLE)
    if not linked:
        return discard(Discard.NO_ARTISTS)
    if any(
        page_type != ARTIST_PAGE_TYPE or not _ARTIST_ID.fullmatch(browse_id)
        for browse_id, page_type, _ in linked
    ):
        return discard(Discard.ARTIST_ID)
    if len(linked) > MAX_ARTISTS:
        return discard(Discard.TOO_MANY_ARTISTS)
    if unlinked:
        return discard(Discard.ARTIST_UNLINKED)
    artists = tuple((browse_id, name.strip()) for browse_id, _, name in linked)
    if any(
        not name or len(name) > MAX_TITLE or name.casefold() in FORBIDDEN_ARTIST_NAMES
        for _, name in artists
    ):
        return discard(Discard.ARTIST_NAME)
    if not cover.startswith("https://") or len(cover) > MAX_URL:
        return discard(Discard.COVER)
    return ReleaseCard(
        album_id,
        audio_playlist_id,
        type_text,
        title,
        tuple(names),
        release=Release(
            album_id=album_id,
            audio_playlist_id=audio_playlist_id,
            type=RELEASE_TYPES[type_text],
            title=title,
            artists=artists,
            thumbnail_url=cover,
        ),
    )


def mark_repeated_releases(cards: Sequence[ReleaseCard]) -> list[ReleaseCard]:
    # The first card of an album that is saved keeps its state; the rest are
    # repeats.
    seen: set[str] = set()
    out: list[ReleaseCard] = []
    for card in cards:
        if card.release is not None:
            if card.album_id in seen:
                card = replace(card, release=None, reason=Discard.REPEATED)
            seen.add(card.album_id)
        out.append(card)
    return out


RELEASE_CSV_HEADER = (
    "orden",
    "estado",
    "tipo",
    "titulo",
    "artistas",
    "id_album",
    "id_playlist_audio",
)


def write_release_csv(
    reports_dir: Path, now: datetime, cards: Iterable[ReleaseCard]
) -> Path:
    reports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = reports_dir / f"release-ingest-{now.strftime('%Y%m%d-%H%M%S')}.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(RELEASE_CSV_HEADER)
        for order, card in enumerate(cards, start=1):
            state = "se guarda" if card.release is not None else card.reason
            writer.writerow(
                [
                    order,
                    _csv_safe(str(getattr(state, "value", state))),
                    _csv_safe(card.type_text),
                    _csv_safe(card.title),
                    _csv_safe(", ".join(card.artist_names)),
                    _csv_safe(card.album_id),
                    _csv_safe(card.audio_playlist_id),
                ]
            )
    return path


def write_release_summary(out: TextIO, cards: Sequence[ReleaseCard]) -> None:
    counts = {reason: 0 for reason in Discard}
    for card in cards:
        if card.reason is not None:
            counts[card.reason] += 1
    parts = [f"{reason.value}={counts[reason]}" for reason in Discard]
    saved = sum(1 for card in cards if card.release is not None)
    out.write(
        " ".join([f"total: leidas={len(cards)}", *parts, f"se guardan={saved}"]) + "\n"
    )


def _release_abort_reasons(cards: Sequence[ReleaseCard]) -> list[str]:
    read = len(cards)
    kept = sum(1 for card in cards if card.release is not None)
    if not cards:
        return ["no cards on the page"]
    if not kept:
        return ["no card passed"]
    reasons = []
    if kept / read < MIN_KEPT_RATIO:
        reasons.append(
            f"only {kept} of {read} cards pass ({100 * kept / read:.1f}%), "
            f"under the minimum of {MIN_KEPT_RATIO:.0%}"
        )
    if kept > MAX_RELEASES:
        reasons.append(f"{kept} releases pass, over the back's limit of {MAX_RELEASES}")
    return reasons


def build_release_payload(cards: Sequence[ReleaseCard]) -> dict[str, Any]:
    return {
        "releases": [
            card.release.to_row() for card in cards if card.release is not None
        ]
    }


def release_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {"releases": len(payload["releases"])}


# --- Kinds -------------------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    session: Any
    provider: Any
    data_dir: Path
    out: TextIO
    now: datetime
    sleep: Callable[[float], None]
    uniform: Callable[[float, float], float]
    breaker: ProviderBreaker


@dataclass(frozen=True)
class Collected:
    # Reasons to abort: when there is any, nothing is sent and build_payload
    # is never called.
    abort_reasons: list[str]
    build_payload: Callable[[], dict[str, Any]]
    # Slugs left out of the submission on purpose; the run exits 3 when the
    # rest goes through.
    skipped: tuple[str, ...] = ()


def _no_payload() -> dict[str, Any]:
    raise AssertionError("a run that aborts builds no payload")


def collect_genres(ctx: RunContext) -> Collected | None:
    session, provider, data_dir, out, now, sleep = (
        ctx.session,
        ctx.provider,
        ctx.data_dir,
        ctx.out,
        ctx.now,
        ctx.sleep,
    )
    sources = parse_genre_sources(
        fetch_json(
            session,
            f"{RAW_BASE_URL}/genre_sources.json",
            missing_ok=False,
            sleep=sleep,
        )
    )
    if not sources:
        logger.error("nothing to ingest")
        return None
    # A missing overrides file (404) is the same as an empty one.
    overrides = parse_overrides(
        fetch_json(
            session, f"{RAW_BASE_URL}/overrides.json", missing_ok=True, sleep=sleep
        )
        or {}
    )

    cache_path = data_dir / "cache.json"
    cache = load_cache(cache_path)
    provider = provider if provider is not None else build_provider()

    breaker = ctx.breaker
    playlists = read_playlists(
        provider, sources, sleep=sleep, uniform=ctx.uniform, breaker=breaker
    )
    resolver = Resolver(
        provider,
        overrides=overrides,
        cache=cache,
        sleep=sleep,
        uniform=ctx.uniform,
        breaker=breaker,
    )
    resolved = resolve_all(resolver, playlists, sleep=sleep)
    if breaker.is_open():
        logger.error(
            "provider cut off after %d exhausted calls: %d source playlists and "
            "%d songs were cut off without finishing their requests",
            breaker.errors,
            breaker.refused.get("source playlist reading", 0),
            breaker.refused.get("resolution pool", 0),
        )

    # The new "oficial" results go to the cache even if the run aborts later.
    for resolution in resolved:
        if resolution.status is Status.OFFICIAL and resolution.track is not None:
            cache[resolution.video_id] = resolution.track
    save_cache(cache_path, cache)

    csv_path = write_csv(data_dir / "reports", now, playlists)
    logger.info("report written to %s", csv_path)
    write_summary(out, playlists)
    problems = genre_problems(playlists)
    for slug, reasons in problems.items():
        out.write(f"no se envia {slug}: {'; '.join(reasons)}\n")
        logger.warning("genre not sent (%s): %s", slug, "; ".join(reasons))
    sendable = [p for p in playlists if p.genre not in problems]
    return Collected(
        [] if sendable else _abort_reasons(playlists),
        lambda: build_genre_payload(sendable),
        skipped=tuple(problems),
    )


def collect_releases(ctx: RunContext) -> Collected | None:
    provider = (
        ctx.provider
        if ctx.provider is not None
        else build_provider(language=RELEASES_LANGUAGE)
    )
    # _send_request is a private method of the library (the second use in the
    # repo, after the artist search): the public methods return parsed
    # objects that drop what this kind needs. The version in requirements.txt
    # and test/core/test_search_provider.py pin it. The body is built in each
    # attempt: the library mutates it.
    ctx.breaker.enter("releases page")
    try:
        page = call_provider(
            lambda: provider._send_request(
                "browse", {"browseId": NEW_RELEASES_BROWSE_ID}
            ),
            sleep=ctx.sleep,
            uniform=ctx.uniform,
            breaker=ctx.breaker,
        )
    except ProviderFailed as exc:
        logger.warning("new releases page not read: %s", _cause(exc))
        write_release_summary(ctx.out, [])
        return Collected(["new releases page not read"], _no_payload)
    items = grid_items(page)
    if items is None:
        write_release_summary(ctx.out, [])
        return Collected(["the new releases page has an unexpected shape"], _no_payload)

    cards = mark_repeated_releases([parse_release_card(item) for item in items])
    if cards:
        csv_path = write_release_csv(ctx.data_dir / "reports", ctx.now, cards)
        logger.info("report written to %s", csv_path)
    write_release_summary(ctx.out, cards)
    reasons = _release_abort_reasons(cards)
    for reason in reasons:
        ctx.out.write(f"aborta: {reason}\n")
    return Collected(reasons, lambda: build_release_payload(cards))


@dataclass(frozen=True)
class Kind:
    name: str
    endpoint: str
    collect: Callable[[RunContext], Collected | None]
    counts: Callable[[dict[str, Any]], dict[str, int]]


KINDS = {
    "genres": Kind(
        name="genres",
        endpoint="/ingest/genres",
        collect=collect_genres,
        counts=genre_counts,
    ),
    "releases": Kind(
        name="releases",
        endpoint="/ingest/releases",
        collect=collect_releases,
        counts=release_counts,
    ),
}


# --- Main --------------------------------------------------------------------


def build_provider(language: str | None = None) -> Any:
    # The timeout rides on the session, as the library documents.
    session = requests.Session()
    session.request = functools.partial(session.request, timeout=PROVIDER_TIMEOUT)
    if language is None:
        return Provider(requests_session=session)
    return Provider(requests_session=session, language=language)


def _library_version() -> str:
    try:
        return importlib.metadata.version(Provider.__module__.partition(".")[0])
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collector.py",
        description="Collects playlists and posts them to the back.",
    )
    parser.add_argument("kind", choices=sorted(KINDS))
    parser.add_argument(
        "--write",
        action="store_true",
        help="send the result to the back (without it, nothing is sent)",
    )
    return parser


def _abort_reasons(playlists: Sequence[PlaylistResult]) -> list[str]:
    reasons = []
    unread = sum(1 for p in playlists if not p.read)
    errors = sum(
        1 for p in playlists for r in p.resolutions if r.status is Status.PROVIDER_ERROR
    )
    empty = sum(1 for p in playlists if p.read and not p.saved())
    if unread:
        reasons.append(f"{unread} source playlists not read")
    if errors:
        reasons.append(f"{errors} provider errors")
    if empty:
        reasons.append(f"{empty} playlists with no saved songs")
    return reasons


def genre_problems(playlists: Sequence[PlaylistResult]) -> dict[str, list[str]]:
    # A genre goes whole or does not go: the back's function deletes the
    # playlists of a genre in the body that are not in it. Only the genres with
    # reasons are returned, in the order of the sources file.
    problems: dict[str, list[str]] = {}
    for slug in dict.fromkeys(p.genre for p in playlists):
        reasons = _abort_reasons([p for p in playlists if p.genre == slug])
        if reasons:
            problems[slug] = reasons
    return problems


def main(
    argv: Sequence[str] | None = None,
    *,
    config_path: Path = ENV_FILE,
    data_dir: Path = DATA_DIR,
    session: Any = None,
    provider: Any = None,
    out: TextIO = sys.stdout,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    uniform: Callable[[float, float], float] = random.uniform,
    breaker: ProviderBreaker | None = None,
) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    try:
        return _run(
            KINDS[args.kind],
            write=args.write,
            config_path=config_path,
            data_dir=data_dir,
            session=session if session is not None else requests.Session(),
            provider=provider,
            out=out,
            now=now or datetime.now(UTC),
            sleep=sleep,
            uniform=uniform,
            breaker=breaker if breaker is not None else ProviderBreaker(),
        )
    except ConfigError as exc:
        logger.error("refusing to run: %s", exc)
    except OSError as exc:
        logger.error("refusing to run: %s", type(exc).__name__)
    except SendFailed as exc:
        logger.error("send failed: %s", exc)
    except FetchFailed as exc:
        logger.error("could not download an input file: %s", _cause(exc))
    return 1


def _run(
    kind: Kind,
    *,
    write: bool,
    config_path: Path,
    data_dir: Path,
    session: Any,
    provider: Any,
    out: TextIO,
    now: datetime,
    sleep: Callable[[float], None],
    uniform: Callable[[float, float], float],
    breaker: ProviderBreaker,
) -> int:
    config = read_config(config_path)
    logger.info("provider library version %s", _library_version())
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)

    ctx = RunContext(
        session=session,
        provider=provider,
        data_dir=data_dir,
        out=out,
        now=now,
        sleep=sleep,
        uniform=uniform,
        breaker=breaker,
    )
    collected = kind.collect(ctx)
    if collected is None:
        return 1
    if collected.abort_reasons:
        logger.error(
            "aborting, nothing is sent: %s", "; ".join(collected.abort_reasons)
        )
        return 1
    # Built on a dry run too: a submission the back would reject (a limit, a
    # missing title) must not exit 0.
    payload = collected.build_payload()
    expected = kind.counts(payload)
    if not write:
        return EXIT_PARTIAL if collected.skipped else 0
    data = send(session, config, kind.endpoint, payload, sleep=sleep)
    if data != expected:
        logger.error("the back accepted a different count than the one sent")
        return 1
    out.write("sent: " + " ".join(f"{k}={v}" for k, v in expected.items()) + "\n")
    return EXIT_PARTIAL if collected.skipped else 0


def configure_logging(data_dir: Path) -> None:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = RotatingFileHandler(
        data_dir / "collector.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    # Warnings and errors also go to stderr so cron mails or logs them.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)


if __name__ == "__main__":
    configure_logging(DATA_DIR)
    raise SystemExit(main())
