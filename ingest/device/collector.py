# INFO: Collector of the playlist ingest. Runs on the owner's device, not in the app: reads source playlists, resolves each video to its audio and posts the result to the back.

# One self-contained file on purpose: it is downloaded with curl onto the
# phone, so it imports only the standard library, requests and the provider
# library. Nothing from the rest of the repo (no core/, models/, pydantic,
# supabase, no Settings): the phone holds no database key, only the ingest
# token. It runs on the Python of Termux (3.11 or later), not on the 3.14 of
# the repo; pyproject.toml sets the ruff target for this folder accordingly.
#
# Usage (cron friendly, never asks anything, never reads stdin):
#
#     python collector.py genres [--write]
#
# Without --write nothing is sent to the back (dry run). Exit codes: 0 the run
# is complete, 1 any failure, 2 bad arguments. The summary goes to stdout, the
# log and the CSV to ~/.beatly-collector/. See docs/ingest.md.
#
# Adding another kind of ingest (releases) means one more entry in KINDS, its
# parser and payload builder, and its endpoint in the back; everything else
# here is shared.

from __future__ import annotations

import argparse
import csv
import functools
import importlib.metadata
import ipaddress
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
# Provider calls: 3 attempts, waits of 1 s and 2 s. The timeouts are the
# (connect, read) ones the back uses for the same provider.
PROVIDER_ATTEMPTS = 3
PROVIDER_BASE_DELAY = 1.0
PROVIDER_TIMEOUT = (3.05, 10)
# Reading the versioned input files from the public repo.
FETCH_TIMEOUT = (10, 30)
# Sending to the back: 4 attempts, waits of 5, 15 and 45 s. The read timeout is
# above the largest INGEST_RPC_TIMEOUT (300 s) so the back answers first.
SEND_DELAYS = (5, 15, 45)
SEND_TIMEOUT = (10, 330)
SEND_RETRY_STATUSES = (502, 503, 504)

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
MAX_PLAYLISTS_PER_GENRE = 20
MAX_TRACKS_PER_PLAYLIST = 300
MAX_TRACKS = 10_000
ID_PATTERN = r"[A-Za-z0-9_-]+"
SLUG_PATTERN = r"[^\s,()\"'\\]+"
_ID = re.compile(ID_PATTERN)
_SLUG = re.compile(SLUG_PATTERN)

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


def call_with_retries(
    fn: Callable[[], Any],
    *,
    retry_on: tuple[type[BaseException], ...] = PROVIDER_RETRYABLE,
    failure: type[CollectorError] = ProviderFailed,
    attempts: int = PROVIDER_ATTEMPTS,
    base_delay: float = PROVIDER_BASE_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    # Waits base_delay * 2 ** (n - 1) after attempt n. The failure raised at
    # the end is chained to the last error, but its text is the collector's.
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if attempt < attempts:
                sleep(base_delay * 2 ** (attempt - 1))
    raise failure("retries exhausted") from last


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
    ) -> None:
        self._provider = provider
        self._overrides = overrides
        self._cache = cache
        self._sleep = sleep

    def _call(self, fn: Callable[[], Any]) -> Any:
        return call_with_retries(fn, sleep=self._sleep)

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
) -> list[PlaylistResult]:
    # One by one, before the pool. A playlist that cannot be read after the
    # retries is marked as not read and the run goes on (it aborts later).
    results: list[PlaylistResult] = []
    for genre in genres:
        for source in genre.playlists:
            try:
                page = call_with_retries(
                    lambda s=source: provider.get_playlist(s.source, limit=None),
                    sleep=sleep,
                )
                tracks = page["tracks"]
                if not isinstance(tracks, list):
                    raise TypeError("tracks is not a list")
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
    resolver: Resolver, playlists: Sequence[PlaylistResult]
) -> list[Resolution]:
    # A single pool for every item of every playlist, in source order.
    # Returns every resolution of the run, in source order.
    tasks = [(p, item) for p in playlists if p.read for item in p.items]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(resolver.resolve, item) for _, item in tasks]
        try:
            resolved = [future.result() for future in futures]
        finally:
            for future in futures:
                future.cancel()
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


# --- Kinds -------------------------------------------------------------------


@dataclass(frozen=True)
class Kind:
    name: str
    sources_file: str
    parse_sources: Callable[[Any], list[GenreSource]]
    endpoint: str
    build_payload: Callable[[Sequence[PlaylistResult]], dict[str, Any]]
    counts: Callable[[dict[str, Any]], dict[str, int]]


KINDS = {
    "genres": Kind(
        name="genres",
        sources_file="genre_sources.json",
        parse_sources=parse_genre_sources,
        endpoint="/ingest/genres",
        build_payload=build_genre_payload,
        counts=genre_counts,
    )
}


# --- Main --------------------------------------------------------------------


def build_provider() -> Any:
    # The timeout rides on the session, as the library documents.
    session = requests.Session()
    session.request = functools.partial(session.request, timeout=PROVIDER_TIMEOUT)
    return Provider(requests_session=session)


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
) -> int:
    config = read_config(config_path)
    logger.info("provider library version %s", _library_version())
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)

    sources = kind.parse_sources(
        fetch_json(
            session,
            f"{RAW_BASE_URL}/{kind.sources_file}",
            missing_ok=False,
            sleep=sleep,
        )
    )
    if not sources:
        logger.error("nothing to ingest")
        return 1
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

    playlists = read_playlists(provider, sources, sleep=sleep)
    resolver = Resolver(provider, overrides=overrides, cache=cache, sleep=sleep)
    resolved = resolve_all(resolver, playlists)

    # The new "oficial" results go to the cache even if the run aborts later.
    for resolution in resolved:
        if resolution.status is Status.OFFICIAL and resolution.track is not None:
            cache[resolution.video_id] = resolution.track
    save_cache(cache_path, cache)

    csv_path = write_csv(data_dir / "reports", now, playlists)
    logger.info("report written to %s", csv_path)
    write_summary(out, playlists)

    reasons = _abort_reasons(playlists)
    if reasons:
        logger.error("aborting, nothing is sent: %s", "; ".join(reasons))
        return 1
    # Built on a dry run too: a submission the back would reject (a limit, a
    # missing title) must not exit 0.
    payload = kind.build_payload(playlists)
    expected = kind.counts(payload)
    if not write:
        return 0
    data = send(session, config, kind.endpoint, payload, sleep=sleep)
    if data != expected:
        logger.error("the back accepted a different count than the one sent")
        return 1
    out.write("sent: " + " ".join(f"{k}={v}" for k, v in expected.items()) + "\n")
    return 0


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
