# test/routes/test_recommendations.py
#
# Tests for the personalized recommendations endpoints.
#
# Tested:
# - GET /recommendations returns the stored set of the caller: the four lists
#   and the seed sections by rank, the deprecated flat lists, the reason and
#   seed, the image tiers and both timestamps, reading only the caller's rows of the for_you bucket
# - GET with no stored set returns 200 with every list empty and both
#   timestamps null, without touching the provider; an expired set is
#   returned as it is
# - GET database failure returns 502, a database timeout 504, a stored row
#   that does not fit the model 502 (never an empty list), both with no-store
# - POST /recommendations/generate with five favorites stores 19 artists and
#   29 albums (48 rows) with their section, ranked in the order of the screen,
#   with no favorite and no top-90-days artist among them, in one call to
#   the replacing function
# - POST fills the albums for you, three seed sections (the first three
#   seeds, artists and albums alternating), the recommended albums and the
#   recommended artists, with no artist or album id repeated, plus the
#   deprecated flat lists with every item; the response has the documented
#   shape and equals the GET that follows
# - POST with few seeds returns only the sections it can fill; a section
#   without albums stays partial, one without candidates is left out and the
#   next seed does not take its place; little content gives shorter lists
# - POST regenerates a still valid set stored before the sections; GET of
#   such a set returns the four new lists empty and the rest as stored
# - GET with a row whose section does not fit returns 502
# - POST never recommends an artist played 3 or more times in 90 days (the
#   threshold comes from settings and the read uses the same window as the
#   top-N); one with fewer plays stays recommendable
# - POST keeps one edition of each album in the whole response (same artist,
#   same base title, in the albums for you, the sections, the recommended
#   albums and the deprecated list) and never recommends a soundtrack; with
#   only soundtracks on offer the album lists are empty (200) and the artists
#   stay
# - POST gives an album the provider sends without artists the artist of its
#   page (in the four lists and in the stored payload) and leaves the albums
#   with artists as they are; never recommends an album that the page of an
#   album seed sends without artists; keeps one edition of an album whose
#   page sends no artists
# - POST played-often read failure returns 502 with no provider call and no
#   write
# - POST with a set that is still valid returns it and calls neither the
#   provider nor the replacing function; an expired one is replaced
# - POST without favorites or plays returns the empty set and calls nobody
# - POST with exhausted similars returns fewer artists (200)
# - POST never recommends a favorite that is not a seed (a sixth favorite)
# - POST with seeds but no candidate at all returns the empty set and writes
#   nothing, leaving the stored set as it is
# - POST with a play history uses the listened artists and the listened
#   albums as seeds (album seeds close the list)
# - POST counts the plays since the previous generated_at (or the seed
#   window the first time) and the tier sets valid_until
# - POST skips a seed, a similar or a history album the provider confirms is
#   gone (a KeyError plus the probe), logs one INFO line and still fills
#   19 + 29; with every seed gone it returns the empty set and writes nothing
# - POST never probes a server error or a timeout, and a KeyError the probe
#   does not confirm is a 502 with no write; a failing probe is 502 or 504
# - POST provider failure, timeout or album seed failure return 502 or 504
#   and write nothing; a failing replacement, an anomalous count and a
#   failing input read return 502
# - The provider calls of the workers share the 12 s request budget
# - Only the images of the response get a color read (the pages of the
#   workers are not noted in the color scope)
# - No token returns 401; a user id in the body or the query is ignored
# - Cache-Control is private, no-cache on success and no-store on errors
# - Both endpoints carry the stored color of their images
#
# What is covered:
# - Happy path, expected empty state, upstream failure and timeout, the
#   atomic replacement contract, the id rules, the skipping of missing
#   seeds and the budget wiring. The real queries and functions are in
#   test/integration/test_recommendations_integration.py
#
# Run with: pytest test/routes/test_recommendations.py -v
#
# SEE: routes/recommendations.py, services/recommendations_service.py

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import requests
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core import provider_session
from core.auth import get_current_user_id
from core.cache import get_redis
from core.config import settings
from core.database import get_db
from core.provider_session import ProviderSession
from core.search_provider import PROVIDER_ERRORS, get_search_provider
from test.fake_provider_http import FakeAdapter, FakeClock, body_response

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
_NO_STORE = "no-store"
_PRIVATE = "private, no-cache"

_OLD_GENERATED_AT = "2020-01-01T00:00:00+00:00"
_OLD_VALID_UNTIL = "2020-01-31T00:00:00+00:00"
_FAR_VALID_UNTIL = "2999-01-01T00:00:00+00:00"

_MISSING_BROWSE = {
    "responseContext": {},
    "trackingParams": "t",
    "microformat": {"microformatDataRenderer": {"noindex": True}},
}
_EXISTING_BROWSE = {
    "responseContext": {},
    "trackingParams": "t",
    "contents": {},
    "microformat": {
        "microformatDataRenderer": {
            "urlCanonical": "https://example.com/x",
            "title": "t",
        }
    },
}

_SEEDS = [f"UCseed{i}" for i in range(1, 6)]


def _thumb(name: str) -> str:
    return f"https://lh3.googleusercontent.com/{name}=w60-h60-l90-rj"


# --- Fakes -------------------------------------------------------------------


class _Query:
    def __init__(self, db: FakeRecommendationsDb, table: str):
        self._db = db
        self._table = table
        self.filters: dict[str, object] = {}
        self.columns = ""
        self.orders: list[str] = []
        self.max_rows: int | None = None

    def select(self, columns, **_):
        self.columns = columns
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def order(self, column, desc=False):
        self.orders.append(column)
        return self

    def limit(self, count):
        self.max_rows = count
        return self

    def execute(self):
        self._db.queries.append(self)
        error = self._db.errors.get(self._table)
        if error is not None:
            raise error
        return SimpleNamespace(data=self._db.read(self))


class _Rpc:
    def __init__(self, db: FakeRecommendationsDb, name: str, params: dict):
        self._db = db
        self._name = name
        self._params = params

    def execute(self):
        self._db.rpcs.append((self._name, self._params))
        error = self._db.errors.get(self._name)
        if error is not None:
            raise error
        return SimpleNamespace(data=self._db.call(self._name, self._params))


class FakeRecommendationsDb:
    # Imitates the postgrest chain the service uses and the four functions of
    # migration 047, by name. errors maps a table or a function name to the
    # exception its execute() raises.
    # The tests keep the play threshold at 2 or more: with 1 the call that
    # reads the artists played often is not told apart from the top-N one.
    def __init__(
        self,
        favorites=(),
        listened=(),
        known=(),
        albums=(),
        plays=0,
        played=(),
        fail_played_often: Exception | None = None,
    ):
        self.rows: list[dict] = []
        self.favorites = list(favorites)
        self.listened = list(listened)
        self.known = list(known)
        self.albums = list(albums)
        self.plays = plays
        self.played = list(played)
        self.fail_played_often = fail_played_often
        self.errors: dict[str, Exception] = {}
        self.replace_count: int | None = None
        self.queries: list[_Query] = []
        self.rpcs: list[tuple[str, dict]] = []

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params, **_):
        return _Rpc(self, name, params)

    def seed_rows(
        self,
        user_id,
        *,
        generated_at,
        valid_until,
        bucket="for_you",
        sectioned=True,
    ):
        # sectioned=False is a set stored before the sections (#227): four
        # rows whose payload has no `section`. sectioned=True is a set with
        # a row in every list and two seed sections.
        layout = [
            ("artist", 1, "artist", {"section": "seed_sections", "section_index": 0}),
            ("artist", 2, "album", {"section": "seed_sections", "section_index": 1}),
            ("album", 1, "artist", {"section": "albums_for_you"}),
            ("album", 2, "artist", {"section": "seed_sections", "section_index": 0}),
        ]
        if sectioned:
            layout += [
                ("artist", 3, "artist", {"section": "recommended_artists"}),
                (
                    "album",
                    3,
                    "artist",
                    {"section": "seed_sections", "section_index": 1},
                ),
                ("album", 4, "artist", {"section": "recommended_albums"}),
            ]
        else:
            layout = [(t, r, st, {}) for t, r, st, _ in layout]
        for item_type, rank, seed_type, placement in layout:
            # The items of a seed section share its seed.
            seed_no = (
                placement["section_index"] + 1 if "section_index" in placement else rank
            )
            self.rows.append(
                {
                    "user_id": user_id,
                    "bucket": bucket,
                    "item_type": item_type,
                    "item_id": f"old-{item_type}-{rank}",
                    "rank": rank,
                    "seed_type": seed_type,
                    "seed_id": f"old-seed-{seed_no}",
                    "seed_name": f"Old seed {seed_no}",
                    "seed_thumb": _thumb(f"seed{seed_no}"),
                    "payload": (
                        {"name": f"Old {rank}", "thumbnail_url": _thumb(f"a{rank}")}
                        if item_type == "artist"
                        else {
                            "title": f"Old album {rank}",
                            "thumbnail_url": _thumb(f"b{rank}"),
                            "artists": [{"id": "UCold", "name": "Old"}],
                            "year": "2020",
                            "audio_playlist_id": f"OLAK5uy_old{rank}",
                        }
                    )
                    | placement,
                    "source": "similar_v1",
                    "generated_at": generated_at,
                    "valid_until": valid_until,
                }
            )

    def read(self, query: _Query):
        if query._table == "library_items":
            assert query.filters["kind"] == "artist"
            return [{"external_id": i} for i in self.favorites]
        assert query._table == "user_recommendations"
        rows = [
            row
            for row in self.rows
            if row["user_id"] == query.filters["user_id"]
            and row["bucket"] == query.filters["bucket"]
        ]
        rows.sort(key=lambda r: (r["item_type"], r["rank"]))
        columns = [c.strip() for c in query.columns.split(",")]
        return [{c: row[c] for c in columns} for row in rows]

    def call(self, name, params):
        if name == "get_top_played_artists":
            since = datetime.fromisoformat(params["p_since"])
            # The exclusion window (90 days) is the longer one.
            wide = datetime.now(UTC) - since > timedelta(days=60)
            min_plays = params["p_min_plays"]
            if not wide:
                assert min_plays == 1
                ids = self.listened
            elif min_plays == 1:
                ids = self.known
            else:
                if self.fail_played_often is not None:
                    raise self.fail_played_often
                return [
                    {"artist_id": i, "play_count": n}
                    for i, n in self.played
                    if n >= min_plays
                ][: params["p_limit"]]
            return [{"artist_id": i, "play_count": 1} for i in ids[: params["p_limit"]]]
        if name == "get_top_played_albums":
            return [
                {"album_id": i, "play_count": 1}
                for i in self.albums[: params["p_limit"]]
            ]
        if name == "count_plays_since":
            return self.plays
        assert name == "replace_user_recommendations"
        self.rows = [r for r in self.rows if r["user_id"] != params["p_user_id"]]
        for row in params["p_rows"]:
            self.rows.append(
                {
                    **row,
                    "user_id": params["p_user_id"],
                    "bucket": params["p_bucket"],
                    "source": params["p_source"],
                    "generated_at": params["p_generated_at"],
                    "valid_until": params["p_valid_until"],
                }
            )
        if self.replace_count is not None:
            return self.replace_count
        return len(params["p_rows"])

    def rpc_params(self, name):
        return [params for called, params in self.rpcs if called == name]


def _artist_page(name, related=(), albums=(), extras=False, titles=None, unlinked=()):
    page = {
        "name": name,
        "thumbnails": [{"url": _thumb(name)}],
        "related": {
            "results": [
                {
                    "browseId": r,
                    "title": f"Name {r}",
                    "thumbnails": [{"url": _thumb(r)}],
                }
                for r in related
            ]
        },
        "albums": {
            "results": [
                {
                    "browseId": a,
                    "title": (titles or {}).get(a, f"Title {a}"),
                    "artists": (
                        [] if a in unlinked else [{"id": "UCsome", "name": "Some"}]
                    ),
                    "year": "2021",
                    "audioPlaylistId": f"OLAK5uy_{a}",
                    "isExplicit": False,
                    "thumbnails": [{"url": _thumb(a)}],
                }
                for a in albums
            ]
        },
    }
    if extras:
        # Images that are in the page but never in the response.
        page["songs"] = {
            "results": [
                {
                    "videoId": "v1",
                    "title": "A song",
                    "artists": [{"id": "UCsome", "name": "Some"}],
                    "thumbnails": [{"url": _thumb("SONGIMG" + name)}],
                }
            ]
        }
        page["singles"] = {
            "results": [
                {
                    "browseId": "MPREb_single",
                    "title": "A single",
                    "thumbnails": [{"url": _thumb("SINGLEIMG" + name)}],
                }
            ]
        }
    return page


def _album_page(album_id, related=(), title=None, titles=None, unlinked=()):
    return {
        "title": title or f"Album {album_id}",
        "duration_seconds": 100,
        "thumbnails": [{"url": _thumb(album_id)}],
        "tracks": [],
        "related_recommendations": [
            {
                "browseId": r,
                "title": (titles or {}).get(r, f"Title {r}"),
                "artists": (
                    [] if r in unlinked else [{"id": "UCsome", "name": "Some"}]
                ),
                "year": "2022",
                "audioPlaylistId": f"OLAK5uy_{r}",
                "thumbnails": [{"url": _thumb(r)}],
            }
            for r in related
        ],
    }


def _related_of(i):
    return [f"UCrel{i}x{j}" for j in range(1, 7)]


def _albums_of(similar_id):
    return [f"MPREb_{similar_id}_{k}" for k in range(1, 4)]


def _world(seeds=_SEEDS, related_overrides=None, extras=False):
    # Every seed has six related artists, each with three albums.
    pages = {}
    for i, seed in enumerate(seeds, start=1):
        related = (related_overrides or {}).get(seed, _related_of(i))
        pages[seed] = _artist_page(f"Seed {i}", related=related, extras=extras)
        for similar in related:
            pages.setdefault(
                similar,
                _artist_page(f"Sim {similar}", albums=_albums_of(similar)),
            )
    return pages


def _provider(artists, albums=None, missing=()):
    # get_artist/get_album raise what the library raises for an id that is
    # not there (KeyError on 'contents'); _send_request, the probe, answers
    # the measured missing shape for those ids and fails for any other.
    provider = MagicMock()

    def get_artist(artist_id):
        if artist_id in missing:
            raise KeyError("contents")
        return artists[artist_id]

    def get_album(album_id):
        if album_id in missing:
            raise KeyError("contents")
        return (albums or {})[album_id]

    def send_request(endpoint, body):
        assert endpoint == "browse"
        assert body["browseId"] in missing, body
        return _MISSING_BROWSE

    provider.get_artist.side_effect = get_artist
    provider.get_album.side_effect = get_album
    provider._send_request.side_effect = send_request
    return provider


def _fake_cache():
    cache = MagicMock()
    cache.get.return_value = None
    cache.ttl.return_value = 1234
    return cache


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    for dependency in (
        get_db,
        get_current_user_id,
        get_search_provider,
        get_redis,
    ):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture(autouse=True)
def _default_cache():
    app.dependency_overrides[get_redis] = lambda: _fake_cache()


def _use(db, provider=None, user_id=_USER_ID):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_search_provider] = lambda: provider or _provider({})


def _generate(**kwargs):
    return client.post("/recommendations/generate", **kwargs)


def _count_by_seed(rows):
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["seed_id"]] = counts.get(row["seed_id"], 0) + 1
    return counts


# Artists per seed in the reference world (5 seeds, 6 related each): 19
# artists in rounds, 4 + 4 + 4 + 4 + 3.
_ARTISTS_BY_SEED = {**dict.fromkeys(_SEEDS[:4], 4), _SEEDS[4]: 3}


def _lists(data):
    # Every item of the four lists of the screen, with the list it is in.
    items = [("albums_for_you", a) for a in data["albums_for_you"]]
    for section in data["seed_sections"]:
        items += [("seed_sections", i) for i in section["items"]]
    items += [("recommended_albums", a) for a in data["recommended_albums"]]
    items += [("recommended_artists", a) for a in data["recommended_artists"]]
    return items


def _replace_calls(db):
    return db.rpc_params("replace_user_recommendations")


# --- GET /recommendations ----------------------------------------------------


def test_get_recommendations_returns_the_sections_and_the_flat_lists():
    db = FakeRecommendationsDb()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_FAR_VALID_UNTIL)
    # Stored in reverse and ordered by the rank column; rows of another user
    # and of another bucket never show up.
    db.seed_rows(
        _OTHER_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_FAR_VALID_UNTIL
    )
    db.seed_rows(
        _USER_ID,
        generated_at=_OLD_GENERATED_AT,
        valid_until=_FAR_VALID_UNTIL,
        bucket="2026-W01",
    )
    db.rows.reverse()
    _use(db)

    response = client.get("/recommendations")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == _PRIVATE
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["generated_at"] == _OLD_GENERATED_AT
    assert data["valid_until"] == _FAR_VALID_UNTIL
    assert data.keys() == {
        "albums_for_you",
        "seed_sections",
        "recommended_albums",
        "recommended_artists",
        "artists",
        "albums",
        "generated_at",
        "valid_until",
    }
    assert [a["id"] for a in data["albums_for_you"]] == ["old-album-1"]
    assert [a["id"] for a in data["recommended_albums"]] == ["old-album-4"]
    assert [a["id"] for a in data["recommended_artists"]] == ["old-artist-3"]
    # Sections by section_index, each one with its artists and albums
    # alternating, and the seed of the section with its image.
    assert [s["seed"]["id"] for s in data["seed_sections"]] == [
        "old-seed-1",
        "old-seed-2",
    ]
    assert data["seed_sections"][0]["seed"]["image"]["small"].endswith("-l90-rj")
    assert [(i["type"], i["id"]) for i in data["seed_sections"][0]["items"]] == [
        ("artist", "old-artist-1"),
        ("album", "old-album-2"),
    ]
    assert [(i["type"], i["id"]) for i in data["seed_sections"][1]["items"]] == [
        ("artist", "old-artist-2"),
        ("album", "old-album-3"),
    ]
    assert data["seed_sections"][0]["items"][0].keys() == {
        "type",
        "id",
        "name",
        "thumbnail_url",
        "image",
        "reason",
    }
    assert [a["id"] for a in data["artists"]] == [
        "old-artist-1",
        "old-artist-2",
        "old-artist-3",
    ]
    assert data["artists"][0] == {
        "id": "old-artist-1",
        "name": "Old 1",
        "thumbnail_url": _thumb("a1"),
        "image": {
            "small": "https://lh3.googleusercontent.com/a1=w192-h192-p-l90-rj",
            "medium": "https://lh3.googleusercontent.com/a1=w600-h600-p-l90-rj",
            "large": "https://lh3.googleusercontent.com/a1=w1320-h1320-p-l90-rj",
            "color": None,
        },
        "reason": {
            "type": "similar_to",
            "seed": {
                "type": "artist",
                "id": "old-seed-1",
                "name": "Old seed 1",
                "thumbnail_url": _thumb("seed1"),
                "image": {
                    "small": "https://lh3.googleusercontent.com/seed1=w192-h192-p-l90-rj",
                    "medium": "https://lh3.googleusercontent.com/seed1=w600-h600-p-l90-rj",
                    "large": "https://lh3.googleusercontent.com/seed1=w1320-h1320-p-l90-rj",
                    "color": None,
                },
            },
        },
    }
    assert data["artists"][1]["reason"]["seed"]["type"] == "album"
    album = data["albums"][0]
    assert album["id"] == "old-album-1"
    assert album["title"] == "Old album 1"
    assert album["artists"] == [{"id": "UCold", "name": "Old"}]
    assert album["year"] == "2020"
    assert album["audio_playlist_id"] == "OLAK5uy_old1"
    assert album["reason"]["seed"]["id"] == "old-seed-1"
    assert [a["id"] for a in data["albums"]] == [
        "old-album-1",
        "old-album-2",
        "old-album-3",
        "old-album-4",
    ]
    query = db.queries[0]
    assert query.filters == {"user_id": _USER_ID, "bucket": "for_you"}


def test_get_recommendations_without_rows_returns_the_empty_set():
    db = FakeRecommendationsDb()
    provider = _provider({})
    _use(db, provider)

    response = client.get("/recommendations")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "albums_for_you": [],
            "seed_sections": [],
            "recommended_albums": [],
            "recommended_artists": [],
            "artists": [],
            "albums": [],
            "generated_at": None,
            "valid_until": None,
        },
    }
    provider.get_artist.assert_not_called()
    provider.get_album.assert_not_called()


def test_get_recommendations_returns_an_expired_set_as_is():
    db = FakeRecommendationsDb()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_OLD_VALID_UNTIL)
    provider = _provider({})
    _use(db, provider)

    response = client.get("/recommendations")

    assert response.status_code == 200
    assert response.json()["data"]["valid_until"] == _OLD_VALID_UNTIL
    assert len(response.json()["data"]["artists"]) == 3
    provider.get_artist.assert_not_called()
    assert _replace_calls(db) == []


def test_get_recommendations_of_a_set_without_sections_returns_empty_sections():
    db = FakeRecommendationsDb()
    db.seed_rows(
        _USER_ID,
        generated_at=_OLD_GENERATED_AT,
        valid_until=_FAR_VALID_UNTIL,
        sectioned=False,
    )
    provider = _provider({})
    _use(db, provider)

    response = client.get("/recommendations")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["albums_for_you"] == []
    assert data["seed_sections"] == []
    assert data["recommended_albums"] == []
    assert data["recommended_artists"] == []
    assert [a["id"] for a in data["artists"]] == ["old-artist-1", "old-artist-2"]
    assert [a["id"] for a in data["albums"]] == ["old-album-1", "old-album-2"]
    assert data["generated_at"] == _OLD_GENERATED_AT
    assert data["valid_until"] == _FAR_VALID_UNTIL
    provider.get_artist.assert_not_called()


def _break_row(row, case):
    if case == "unknown_section":
        row["payload"]["section"] = "somewhere"
    elif case == "album_in_recommended_artists":
        row["item_type"] = "album"
        row["payload"] = {"title": "T", "section": "recommended_artists"}
    elif case == "section_without_index":
        row["payload"].pop("section_index", None)
        row["payload"]["section"] = "seed_sections"
    elif case == "index_out_of_range":
        row["payload"]["section"] = "seed_sections"
        row["payload"]["section_index"] = 3
    elif case == "index_in_a_flat_list":
        row["payload"]["section"] = "albums_for_you"
        row["payload"]["section_index"] = 0


@pytest.mark.parametrize(
    "case",
    [
        "unknown_section",
        "album_in_recommended_artists",
        "section_without_index",
        "index_out_of_range",
        "index_in_a_flat_list",
    ],
)
def test_get_recommendations_row_with_a_broken_section_returns_upstream_error(case):
    db = FakeRecommendationsDb()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_FAR_VALID_UNTIL)
    _break_row(next(r for r in db.rows if r["item_id"] == "old-artist-3"), case)
    _use(db)

    response = client.get("/recommendations")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert response.headers["Cache-Control"] == _NO_STORE


def test_get_recommendations_database_failure_returns_upstream_error():
    db = FakeRecommendationsDb()
    db.errors["user_recommendations"] = APIError({"message": "connection refused"})
    _use(db)

    response = client.get("/recommendations")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert response.headers["Cache-Control"] == _NO_STORE


def test_get_recommendations_database_timeout_returns_upstream_timeout():
    db = FakeRecommendationsDb()
    db.errors["user_recommendations"] = httpx.ReadTimeout("timed out")
    _use(db)

    response = client.get("/recommendations")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    assert response.headers["Cache-Control"] == _NO_STORE


def test_get_recommendations_malformed_row_returns_upstream_error():
    db = FakeRecommendationsDb()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_FAR_VALID_UNTIL)
    db.rows[0]["item_type"] = "playlist"
    _use(db)

    response = client.get("/recommendations")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_recommendations_without_token_returns_unauthorized():
    response = client.get("/recommendations")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def _stored_get(db):
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_FAR_VALID_UNTIL)
    _use(db)
    return lambda: client.get("/recommendations")


def test_get_recommendations_carries_the_stored_color(image_color_db, color_checks):
    color_checks.carries(_stored_get(FakeRecommendationsDb()))


def test_get_recommendations_reads_no_colors_when_there_is_nothing_to_read(
    image_color_db, color_checks
):
    _use(FakeRecommendationsDb())

    color_checks.reads_nothing(lambda: client.get("/recommendations"))


def test_get_recommendations_survives_a_failed_color_read(image_color_db, color_checks):
    color_checks.survives_read_failure(_stored_get(FakeRecommendationsDb()))


# --- POST /recommendations/generate -----------------------------------------


def _five_favorites_db(**kwargs):
    return FakeRecommendationsDb(favorites=_SEEDS, **kwargs)


def test_generate_with_five_favorites_fills_every_section_without_repeats():
    # Seed 1's first related is in the top of the last 90 days and seed 2's
    # second is a favorite: both are skipped for the next candidate.
    world = _world(
        related_overrides={
            "UCseed1": ["UCknown", "UCseed2", *_related_of(1)],
            "UCseed3": ["UCseed4", *_related_of(3)],
        }
    )
    provider = _provider(world)
    db = _five_favorites_db(known=["UCknown"])
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == _PRIVATE
    calls = _replace_calls(db)
    assert len(calls) == 1
    params = calls[0]
    assert params["p_user_id"] == _USER_ID
    assert params["p_bucket"] == "for_you"
    assert params["p_source"] == "similar_v1"
    generated = datetime.fromisoformat(params["p_generated_at"])
    assert datetime.fromisoformat(params["p_valid_until"]) - generated == timedelta(
        days=30
    )
    rows = params["p_rows"]
    assert len(rows) == 48
    artists = [r for r in rows if r["item_type"] == "artist"]
    albums = [r for r in rows if r["item_type"] == "album"]
    assert [r["rank"] for r in artists] == list(range(1, 20))
    assert [r["rank"] for r in albums] == list(range(1, 30))
    assert len({r["item_id"] for r in artists}) == 19
    assert len({r["item_id"] for r in albums}) == 29
    assert not {"UCknown", *_SEEDS} & {r["item_id"] for r in artists}
    assert _count_by_seed(artists) == _ARTISTS_BY_SEED
    assert _count_by_seed(albums) == {
        "UCseed1": 7,
        "UCseed2": 7,
        "UCseed3": 7,
        "UCseed4": 4,
        "UCseed5": 4,
    }
    assert {r["seed_type"] for r in rows} == {"artist"}
    first = artists[0]
    assert first["item_id"] == "UCrel1x1"
    assert first["seed_name"] == "Seed 1"
    assert first["payload"] == {
        "name": "Name UCrel1x1",
        "thumbnail_url": "https://lh3.googleusercontent.com/UCrel1x1=w544-h544-p-l90-rj",
        "section": "seed_sections",
        "section_index": 0,
    }
    album = albums[0]
    assert album["payload"].keys() == {
        "title",
        "thumbnail_url",
        "artists",
        "year",
        "audio_playlist_id",
        "section",
    }
    assert album["payload"]["section"] == "albums_for_you"
    assert first["seed_thumb"] == (
        "https://lh3.googleusercontent.com/Seed 1=w544-h544-p-l90-rj"
    )
    # The response is the reread of the stored set, same as the GET.
    assert response.json() == client.get("/recommendations").json()
    data = response.json()["data"]
    assert len(data["albums_for_you"]) == 10
    assert [len(s["items"]) for s in data["seed_sections"]] == [6, 6, 6]
    assert len(data["recommended_albums"]) == 10
    assert len(data["recommended_artists"]) == 10
    # The sections are the first three seeds, in order, and every item of a
    # section says it comes from the seed of the section.
    assert [s["seed"]["id"] for s in data["seed_sections"]] == _SEEDS[:3]
    for section in data["seed_sections"]:
        assert {i["reason"]["seed"]["id"] for i in section["items"]} == {
            section["seed"]["id"]
        }
        assert [i["type"] for i in section["items"]] == ["artist", "album"] * 3
    # No artist and no album appears twice in the response.
    items = _lists(data)
    artist_ids = [
        i["id"]
        for name, i in items
        if name == "recommended_artists" or i.get("type") == "artist"
    ]
    album_ids = [
        i["id"]
        for name, i in items
        if name in ("albums_for_you", "recommended_albums") or i.get("type") == "album"
    ]
    assert len(artist_ids) == len(set(artist_ids)) == 19
    assert len(album_ids) == len(set(album_ids)) == 29
    # The favorites were read for the caller, most recent first.
    favorites_query = next(q for q in db.queries if q._table == "library_items")
    assert favorites_query.filters == {"user_id": _USER_ID, "kind": "artist"}


def test_generate_response_has_the_documented_shape():
    _use(_five_favorites_db(), _provider(_world()))

    response = _generate()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data.keys() == {
        "albums_for_you",
        "seed_sections",
        "recommended_albums",
        "recommended_artists",
        "artists",
        "albums",
        "generated_at",
        "valid_until",
    }
    section = data["seed_sections"][0]
    assert section.keys() == {"seed", "items"}
    assert section["seed"].keys() == {"type", "id", "name", "thumbnail_url", "image"}
    assert section["seed"]["type"] == "artist"
    assert "color" in section["seed"]["image"]
    artist_item = section["items"][0]
    album_item = section["items"][1]
    assert artist_item.keys() == {
        "type",
        "id",
        "name",
        "thumbnail_url",
        "image",
        "reason",
    }
    assert artist_item["type"] == "artist"
    assert album_item.keys() == {
        "type",
        "id",
        "title",
        "artists",
        "year",
        "audio_playlist_id",
        "thumbnail_url",
        "image",
        "reason",
    }
    assert album_item["type"] == "album"
    flat_artist = data["recommended_artists"][0]
    flat_album = data["albums_for_you"][0]
    assert flat_artist.keys() == artist_item.keys() - {"type"}
    assert flat_album.keys() == album_item.keys() - {"type"}
    for item in (artist_item, album_item, flat_artist, flat_album):
        assert "color" in item["image"]
        assert "color" in item["reason"]["seed"]["image"]


def test_generate_keeps_the_deprecated_flat_lists_with_every_item():
    _use(_five_favorites_db(), _provider(_world()))

    response = _generate()

    data = response.json()["data"]
    section_items = [i for s in data["seed_sections"] for i in s["items"]]
    section_artists = [i for i in section_items if i["type"] == "artist"]
    section_albums = [i for i in section_items if i["type"] == "album"]
    assert len(data["artists"]) == 19
    assert len(data["albums"]) == 29
    assert [a["id"] for a in data["artists"]] == [
        i["id"] for i in [*section_artists, *data["recommended_artists"]]
    ]
    assert [a["id"] for a in data["albums"]] == [
        i["id"]
        for i in [
            *data["albums_for_you"],
            *section_albums,
            *data["recommended_albums"],
        ]
    ]
    assert "type" not in data["artists"][0]
    assert "type" not in data["albums"][0]
    assert data["artists"][0].keys() == data["recommended_artists"][0].keys()


def test_generate_never_repeats_an_album_two_seeds_share():
    world = _world()
    world["UCrel1x1"] = _artist_page("Sim A", albums=["MPREb_shared", "MPREb_a2"])
    world["UCrel2x1"] = _artist_page("Sim B", albums=["MPREb_shared", "MPREb_b2"])
    _use(_five_favorites_db(), _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    placed = [name for name, i in _lists(data) if i["id"] == "MPREb_shared"]
    assert placed == ["albums_for_you"]
    assert [a["id"] for a in data["albums"]].count("MPREb_shared") == 1


def test_generate_stores_the_section_of_every_row_in_the_payload():
    db = _five_favorites_db()
    _use(db, _provider(_world()))

    _generate()

    rows = _replace_calls(db)[0]["p_rows"]
    seen = {
        (r["item_type"], r["payload"]["section"], r["payload"].get("section_index"))
        for r in rows
    }
    assert seen == {
        ("artist", "seed_sections", 0),
        ("artist", "seed_sections", 1),
        ("artist", "seed_sections", 2),
        ("artist", "recommended_artists", None),
        ("album", "albums_for_you", None),
        ("album", "seed_sections", 0),
        ("album", "seed_sections", 1),
        ("album", "seed_sections", 2),
        ("album", "recommended_albums", None),
    }


@pytest.mark.parametrize(("count", "recommended"), [(1, 3), (2, 6)])
def test_generate_with_few_seeds_returns_only_the_sections_it_can_fill(
    count, recommended
):
    db = FakeRecommendationsDb(favorites=_SEEDS[:count])
    _use(db, _provider(_world(seeds=_SEEDS[:count])))

    response = _generate()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert [s["seed"]["id"] for s in data["seed_sections"]] == _SEEDS[:count]
    assert len(data["recommended_artists"]) == recommended


def test_generate_section_without_albums_is_partial_and_not_filled_with_artists():
    world = _world()
    for related in _related_of(1):
        world[related] = _artist_page(f"Sim {related}")
    _use(_five_favorites_db(), _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    section = response.json()["data"]["seed_sections"][0]
    assert section["seed"]["id"] == "UCseed1"
    assert [i["type"] for i in section["items"]] == ["artist"] * 3


def test_generate_section_seed_without_candidates_is_left_out():
    world = _world(related_overrides={"UCseed2": []})
    _use(_five_favorites_db(), _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # The fourth seed does not take the place of the second.
    assert [s["seed"]["id"] for s in data["seed_sections"]] == ["UCseed1", "UCseed3"]


def test_generate_section_seed_without_candidates_fetches_only_what_fits():
    world = _world(related_overrides={"UCseed2": []})
    provider = _provider(world)
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200, response.text
    stored = [r for r in _replace_calls(db)[0]["p_rows"] if r["item_type"] == "artist"]
    # 6 in the two sections that have candidates, 10 recommended.
    assert len(stored) == 16
    fetched = [call.args[0] for call in provider.get_artist.call_args_list]
    similars = [artist_id for artist_id in fetched if artist_id not in _SEEDS]
    # No page of a similar artist that is then left out of every list.
    assert len(similars) == len(stored)
    assert set(similars) == {r["item_id"] for r in stored}


def test_generate_with_little_content_returns_shorter_lists():
    seeds = _SEEDS[:3]
    world = _world(
        seeds=seeds,
        related_overrides={seed: [f"UCrel{i}x1"] for i, seed in enumerate(seeds, 1)},
    )
    _use(FakeRecommendationsDb(favorites=seeds), _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["albums_for_you"]) == 9
    assert [len(s["items"]) for s in data["seed_sections"]] == [1, 1, 1]
    assert data["recommended_albums"] == []
    assert data["recommended_artists"] == []


def test_generate_with_a_valid_set_without_sections_regenerates_it():
    db = _five_favorites_db()
    db.seed_rows(
        _USER_ID,
        generated_at=_OLD_GENERATED_AT,
        valid_until=_FAR_VALID_UNTIL,
        sectioned=False,
    )
    provider = _provider(_world())
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200, response.text
    assert len(_replace_calls(db)) == 1
    provider.get_artist.assert_called()
    data = response.json()["data"]
    assert data["generated_at"] != _OLD_GENERATED_AT
    assert len(data["seed_sections"]) == 3


def _played_world():
    return _world(
        related_overrides={"UCseed1": ["UCplayed3", "UCplayed2", *_related_of(1)]}
    )


def _seed1_artists(db):
    rows = _replace_calls(db)[0]["p_rows"]
    artists = [r for r in rows if r["item_type"] == "artist"]
    return artists, [r["item_id"] for r in artists if r["seed_id"] == "UCseed1"]


def test_generate_excludes_artists_played_three_or_more_times_in_ninety_days():
    db = _five_favorites_db(played=[("UCplayed3", 3), ("UCplayed2", 2)])
    _use(db, _provider(_played_world()))

    response = _generate()

    assert response.status_code == 200, response.text
    artists, seed1 = _seed1_artists(db)
    assert "UCplayed3" not in {r["item_id"] for r in artists}
    assert seed1 == ["UCplayed2", "UCrel1x1", "UCrel1x2", "UCrel1x3"]
    assert _count_by_seed(artists) == _ARTISTS_BY_SEED
    calls = db.rpc_params("get_top_played_artists")
    often = [c for c in calls if c["p_min_plays"] == 3]
    assert len(often) == 1
    assert often[0]["p_limit"] == 1000
    assert sorted(c["p_min_plays"] for c in calls) == [1, 1, 3]
    wide = [
        c
        for c in calls
        if datetime.now(UTC) - datetime.fromisoformat(c["p_since"]) > timedelta(days=60)
    ]
    assert len(wide) == 2
    assert wide[0]["p_since"] == wide[1]["p_since"]
    age = datetime.now(UTC) - datetime.fromisoformat(often[0]["p_since"])
    assert timedelta(days=90) <= age <= timedelta(days=90, minutes=1)


def test_generate_played_threshold_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "recommendations_exclude_min_plays", 2)
    db = _five_favorites_db(played=[("UCplayed3", 3), ("UCplayed2", 2)])
    _use(db, _provider(_played_world()))

    response = _generate()

    assert response.status_code == 200, response.text
    artists, seed1 = _seed1_artists(db)
    assert not {"UCplayed3", "UCplayed2"} & {r["item_id"] for r in artists}
    assert seed1 == ["UCrel1x1", "UCrel1x2", "UCrel1x3", "UCrel1x4"]


def test_generate_played_often_read_failure_returns_upstream_error_without_calling_the_provider():
    db = _five_favorites_db(fail_played_often=APIError({"message": "boom"}))
    provider = _provider(_world())
    _use(db, provider)

    response = _generate()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    provider.get_artist.assert_not_called()
    assert _replace_calls(db) == []


def test_generate_never_recommends_a_favorite_that_is_not_a_seed():
    # Six favorites: only the first five are seeds. The sixth sits first in
    # the related list of seed 1, so only the favorites exclusion keeps it out
    # and seed 1 takes its next candidate.
    world = _world(
        related_overrides={"UCseed1": ["UCfav6", *_related_of(1)]},
    )
    db = FakeRecommendationsDb(favorites=[*_SEEDS, "UCfav6"])
    _use(db, _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    rows = _replace_calls(db)[0]["p_rows"]
    artists = [r for r in rows if r["item_type"] == "artist"]
    assert "UCfav6" not in {r["seed_id"] for r in rows}
    assert "UCfav6" not in {r["item_id"] for r in artists}
    assert _count_by_seed(artists) == _ARTISTS_BY_SEED
    assert [r["item_id"] for r in artists if r["seed_id"] == "UCseed1"] == [
        "UCrel1x1",
        "UCrel1x2",
        "UCrel1x3",
        "UCrel1x4",
    ]


def test_generate_without_seeds_returns_the_empty_set_without_calling_anyone():
    db = FakeRecommendationsDb()
    provider = _provider({})
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "albums_for_you": [],
            "seed_sections": [],
            "recommended_albums": [],
            "recommended_artists": [],
            "artists": [],
            "albums": [],
            "generated_at": None,
            "valid_until": None,
        },
    }
    provider.get_artist.assert_not_called()
    provider.get_album.assert_not_called()
    assert _replace_calls(db) == []


def test_generate_with_a_valid_set_returns_it_without_calling_the_provider():
    db = _five_favorites_db()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_FAR_VALID_UNTIL)
    provider = _provider({})
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200
    assert response.json()["data"]["generated_at"] == _OLD_GENERATED_AT
    provider.get_artist.assert_not_called()
    assert _replace_calls(db) == []
    assert [name for name, _ in db.rpcs] == []


def test_generate_replaces_an_expired_set():
    db = _five_favorites_db()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_OLD_VALID_UNTIL)
    _use(db, _provider(_world()))

    response = _generate()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["generated_at"] != _OLD_GENERATED_AT
    assert len(data["artists"]) == 19
    assert all(a["id"].startswith("UCrel") for a in data["artists"])


def test_generate_with_exhausted_similars_returns_fewer_artists():
    world = _world(
        seeds=_SEEDS[:2],
        related_overrides={
            "UCseed1": _related_of(1)[:2],
            "UCseed2": _related_of(2)[:3],
        },
    )
    db = FakeRecommendationsDb(favorites=_SEEDS[:2])
    _use(db, _provider(world))

    response = _generate()

    assert response.status_code == 200
    assert len(response.json()["data"]["artists"]) == 5


def test_generate_with_a_single_seed_fills_with_rounds_of_that_seed():
    world = _world(
        seeds=_SEEDS[:1],
        related_overrides={"UCseed1": ["UCrel1x" + str(j) for j in range(1, 21)]},
    )
    db = FakeRecommendationsDb(favorites=_SEEDS[:1])
    _use(db, _provider(world))

    response = _generate()

    assert response.status_code == 200
    rows = _replace_calls(db)[0]["p_rows"]
    assert _count_by_seed([r for r in rows if r["item_type"] == "artist"]) == {
        "UCseed1": 13
    }


def test_generate_with_history_uses_listened_seeds_and_album_seeds():
    world = _world(seeds=["UCfav1", "UClis1"])
    albums = {
        f"MPREbhist{n}": _album_page(
            f"MPREbhist{n}", related=[f"MPREbrec{n}x{k}" for k in range(1, 4)]
        )
        for n in (1, 2)
    }
    db = FakeRecommendationsDb(
        favorites=["UCfav1"],
        listened=["UClis1", "UCfav1"],
        albums=["MPREbhist1", "MPREbhist2"],
    )
    _use(db, _provider(world, albums))

    response = _generate()

    assert response.status_code == 200, response.text
    rows = _replace_calls(db)[0]["p_rows"]
    artists = [r for r in rows if r["item_type"] == "artist"]
    album_rows = [r for r in rows if r["item_type"] == "album"]
    assert set(_count_by_seed(artists)) == {"UCfav1", "UClis1"}
    assert len(album_rows) == 26
    by_section = {}
    for row in album_rows:
        by_section.setdefault(row["payload"]["section"], []).append(row)
    # The two album seeds contribute first, in the closing slots of the list
    # of the albums for you; the sections only use artist seeds.
    tail = by_section["albums_for_you"][-2:]
    assert len(by_section["albums_for_you"]) == 10
    assert [r["seed_type"] for r in tail] == ["album", "album"]
    assert {r["seed_id"] for r in tail} == {"MPREbhist1", "MPREbhist2"}
    assert tail[0]["seed_name"] == "Album MPREbhist1"
    assert all(r["seed_type"] == "artist" for r in by_section["albums_for_you"][:-2])
    assert all(r["seed_type"] == "artist" for r in by_section["seed_sections"])
    # Their reserve also applies to the recommended albums, with what is left.
    assert [r["seed_type"] for r in by_section["recommended_albums"][-2:]] == [
        "album",
        "album",
    ]
    assert "MPREbhist1" not in {r["item_id"] for r in album_rows}


def test_generate_with_history_albums_above_ten_caps_albums_for_you_at_ten(
    monkeypatch,
):
    monkeypatch.setattr(settings, "recommendations_history_albums", 15)
    world = _world(seeds=["UCfav1"])
    album_ids = [f"MPREbhist{n}" for n in range(1, 4)]
    albums = {
        album_id: _album_page(
            album_id, related=[f"{album_id}x{k}" for k in range(1, 8)]
        )
        for album_id in album_ids
    }
    db = FakeRecommendationsDb(favorites=["UCfav1"], albums=album_ids)
    _use(db, _provider(world, albums))

    response = _generate()

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["albums_for_you"]) == 10
    sections = [r["payload"]["section"] for r in _replace_calls(db)[0]["p_rows"]]
    assert sections.count("albums_for_you") == 10


def test_generate_skips_seed_ids_the_routes_would_reject():
    world = _world(seeds=["UCgood"])
    db = FakeRecommendationsDb(favorites=["bad-id", "UCgood"], albums=["not-an-album"])
    provider = _provider(world)
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200
    provider.get_album.assert_not_called()
    asked = {call.args[0] for call in provider.get_artist.call_args_list}
    assert "bad-id" not in asked


def test_generate_counts_plays_since_the_previous_generation():
    db = _five_favorites_db(plays=20)
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_OLD_VALID_UNTIL)
    _use(db, _provider(_world()))

    response = _generate()

    assert response.status_code == 200
    assert db.rpc_params("count_plays_since") == [
        {"p_user_id": _USER_ID, "p_since": _OLD_GENERATED_AT}
    ]
    params = _replace_calls(db)[0]
    generated = datetime.fromisoformat(params["p_generated_at"])
    assert datetime.fromisoformat(params["p_valid_until"]) - generated == timedelta(
        days=7
    )


def test_first_generation_counts_plays_of_the_seed_window():
    db = _five_favorites_db(plays=5)
    _use(db, _provider(_world()))

    response = _generate()

    assert response.status_code == 200
    since = datetime.fromisoformat(db.rpc_params("count_plays_since")[0]["p_since"])
    generated = datetime.fromisoformat(_replace_calls(db)[0]["p_generated_at"])
    assert abs((generated - since) - timedelta(days=30)) < timedelta(seconds=5)
    valid = datetime.fromisoformat(_replace_calls(db)[0]["p_valid_until"])
    assert valid - generated == timedelta(days=14)


def test_generate_provider_failure_on_a_similar_page_returns_upstream_error_and_writes_nothing():
    world = _world()
    provider = _provider(world)
    original = provider.get_artist.side_effect

    def fail_on_a_similar(artist_id):
        if artist_id not in _SEEDS:
            raise PROVIDER_ERRORS[0]("server error")
        return original(artist_id)

    provider.get_artist.side_effect = fail_on_a_similar
    db = _five_favorites_db()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_OLD_VALID_UNTIL)
    _use(db, provider)

    response = _generate()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert response.headers["Cache-Control"] == _NO_STORE
    assert _replace_calls(db) == []
    provider._send_request.assert_not_called()
    assert len(db.rows) == 7


def test_generate_provider_timeout_returns_upstream_timeout():
    provider = _provider(_world())
    provider.get_artist.side_effect = requests.exceptions.ReadTimeout("slow")
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    assert response.headers["Cache-Control"] == _NO_STORE
    provider._send_request.assert_not_called()
    assert _replace_calls(db) == []


def test_generate_album_seed_failure_returns_upstream_error():
    provider = _provider(_world(seeds=["UCfav1"]))
    provider.get_album.side_effect = PROVIDER_ERRORS[0]("server error")
    db = FakeRecommendationsDb(favorites=["UCfav1"], albums=["MPREbhist1"])
    _use(db, provider)

    response = _generate()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert _replace_calls(db) == []


def test_generate_server_error_is_never_probed():
    provider = _provider(_world())
    provider.get_artist.side_effect = PROVIDER_ERRORS[0]("server error")
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 502
    provider._send_request.assert_not_called()
    assert _replace_calls(db) == []


def test_generate_keyerror_with_an_existing_page_still_returns_upstream_error():
    provider = _provider(_world())
    provider.get_artist.side_effect = KeyError("contents")
    provider._send_request.side_effect = None
    provider._send_request.return_value = _EXISTING_BROWSE
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert _replace_calls(db) == []


def test_generate_probe_failure_returns_upstream_error():
    provider = _provider(_world())
    provider.get_artist.side_effect = KeyError("contents")
    provider._send_request.side_effect = PROVIDER_ERRORS[0]("server error")
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 502
    assert _replace_calls(db) == []


def test_generate_probe_timeout_returns_upstream_timeout():
    provider = _provider(_world())
    provider.get_artist.side_effect = KeyError("contents")
    provider._send_request.side_effect = requests.exceptions.ReadTimeout("slow")
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    assert _replace_calls(db) == []


def test_generate_timeout_is_never_probed():
    provider = _provider(_world())
    provider.get_artist.side_effect = requests.exceptions.ConnectTimeout("slow")
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 504
    provider._send_request.assert_not_called()
    assert _replace_calls(db) == []


def test_generate_with_one_missing_seed_builds_the_sections_from_the_other_four(
    caplog,
):
    provider = _provider(_world(), missing={"UCseed3"})
    db = _five_favorites_db()
    _use(db, provider)

    with caplog.at_level(logging.INFO):
        response = _generate()

    assert response.status_code == 200, response.text
    rows = _replace_calls(db)[0]["p_rows"]
    assert len(rows) == 48
    artists = [r for r in rows if r["item_type"] == "artist"]
    albums = [r for r in rows if r["item_type"] == "album"]
    assert sorted(_count_by_seed(artists).values()) == [4, 5, 5, 5]
    assert "UCseed3" not in _count_by_seed(artists)
    assert len(artists) == 19
    assert len(albums) == 29
    # The sections are the first three seeds that exist: 1, 2 and 4.
    section_seeds = {
        r["payload"]["section_index"]: r["seed_id"]
        for r in rows
        if r["payload"]["section"] == "seed_sections"
    }
    assert section_seeds == {0: "UCseed1", 1: "UCseed2", 2: "UCseed4"}
    assert "UCseed3" not in {r["seed_id"] for r in rows}
    assert "UCseed3" not in {r["item_id"] for r in rows}
    provider._send_request.assert_called_once_with("browse", {"browseId": "UCseed3"})
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    skipped = [r for r in caplog.records if "recommendations skipped" in r.getMessage()]
    assert len(skipped) == 1
    assert skipped[0].levelno == logging.INFO
    assert "UCseed3" in skipped[0].getMessage()
    assert "seed_artist" in skipped[0].getMessage()
    assert _USER_ID not in skipped[0].getMessage()


def test_generate_with_a_missing_similar_takes_the_next_candidate_of_the_same_seed():
    missing = "UCrel1x2"
    provider = _provider(_world(), missing={missing})
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200, response.text
    rows = _replace_calls(db)[0]["p_rows"]
    artists = [r for r in rows if r["item_type"] == "artist"]
    assert len(artists) == 19
    assert missing not in {r["item_id"] for r in artists}
    assert _count_by_seed(artists) == _ARTISTS_BY_SEED
    assert [r["item_id"] for r in artists if r["seed_id"] == "UCseed1"] == [
        "UCrel1x1",
        "UCrel1x3",
        "UCrel1x4",
        "UCrel1x5",
    ]
    asked = [call.args[0] for call in provider.get_artist.call_args_list]
    assert asked.count(missing) == 1


def test_generate_with_a_missing_history_album_gives_its_slot_to_the_artist_seeds():
    world = _world(seeds=_SEEDS[:2])
    albums = {"MPREbhist2": _album_page("MPREbhist2", related=["MPREbrec2x1"])}
    provider = _provider(world, albums, missing={"MPREbhist1"})
    db = FakeRecommendationsDb(
        favorites=_SEEDS[:2], albums=["MPREbhist1", "MPREbhist2"]
    )
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200, response.text
    album_rows = [
        r for r in _replace_calls(db)[0]["p_rows"] if r["item_type"] == "album"
    ]
    assert len(album_rows) == 26
    assert "MPREbhist1" not in {r["seed_id"] for r in album_rows}
    assert [r["seed_id"] for r in album_rows].count("MPREbhist2") == 1
    provider._send_request.assert_called_once_with("browse", {"browseId": "MPREbhist1"})


def test_generate_when_every_seed_is_missing_returns_the_empty_set_without_writing():
    provider = _provider({}, missing=set(_SEEDS[:2]))
    db = FakeRecommendationsDb(favorites=_SEEDS[:2])
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_OLD_VALID_UNTIL)
    _use(db, provider)

    response = _generate()

    assert response.status_code == 200
    assert response.json()["data"] == {
        "albums_for_you": [],
        "seed_sections": [],
        "recommended_albums": [],
        "recommended_artists": [],
        "artists": [],
        "albums": [],
        "generated_at": None,
        "valid_until": None,
    }
    assert _replace_calls(db) == []
    assert len(db.rows) == 7


def test_generate_with_seeds_but_no_candidate_returns_the_empty_set_without_writing():
    # The seed page exists but has no similar artist and no album: zero rows.
    world = _world(seeds=["UCseed1"], related_overrides={"UCseed1": []})
    db = FakeRecommendationsDb(favorites=["UCseed1"])
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_OLD_VALID_UNTIL)
    _use(db, _provider(world))

    response = _generate()

    assert response.status_code == 200
    assert response.json()["data"] == {
        "albums_for_you": [],
        "seed_sections": [],
        "recommended_albums": [],
        "recommended_artists": [],
        "artists": [],
        "albums": [],
        "generated_at": None,
        "valid_until": None,
    }
    assert _replace_calls(db) == []
    assert len(db.rows) == 7


def test_generate_replace_failure_returns_upstream_error():
    db = _five_favorites_db()
    db.errors["replace_user_recommendations"] = APIError({"message": "boom"})
    _use(db, _provider(_world()))

    response = _generate()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert response.headers["Cache-Control"] == _NO_STORE


def test_generate_replace_anomaly_returns_upstream_error():
    db = _five_favorites_db()
    db.replace_count = 29
    _use(db, _provider(_world()))

    response = _generate()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


@pytest.mark.parametrize(
    "failing", ["library_items", "get_top_played_artists", "count_plays_since"]
)
def test_generate_input_read_failure_returns_upstream_error_without_calling_the_provider(
    failing,
):
    db = _five_favorites_db()
    db.errors[failing] = APIError({"message": "boom"})
    provider = _provider(_world())
    _use(db, provider)

    response = _generate()

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    provider.get_artist.assert_not_called()
    assert _replace_calls(db) == []


def test_generate_count_that_is_not_a_number_returns_upstream_error():
    db = _five_favorites_db()
    db.plays = None
    provider = _provider(_world())
    _use(db, provider)

    response = _generate()

    assert response.status_code == 502
    provider.get_artist.assert_not_called()


def test_generate_without_token_returns_unauthorized():
    response = _generate()

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}


def test_generate_ignores_a_user_id_in_body_or_query():
    db = _five_favorites_db()
    _use(db, _provider(_world()))

    response = _generate(
        params={"user_id": _OTHER_USER_ID}, json={"user_id": _OTHER_USER_ID}
    )

    assert response.status_code == 200
    assert {q.filters["user_id"] for q in db.queries} == {_USER_ID}
    assert {p["p_user_id"] for _, p in db.rpcs} == {_USER_ID}
    assert {r["user_id"] for r in db.rows} == {_USER_ID}


def test_generate_success_sends_private_no_cache():
    _use(_five_favorites_db(), _provider(_world()))

    response = _generate()

    assert response.headers["Cache-Control"] == _PRIVATE


def test_generate_worker_calls_share_the_request_budget(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(provider_session, "_now", clock)
    # One worker keeps the order deterministic: the second call, made from a
    # worker, spends the budget and the third never leaves.
    monkeypatch.setattr(settings, "recommendations_parallel_calls", 1)
    adapter = FakeAdapter(lambda request: (clock.advance(7), body_response(request))[1])
    session = ProviderSession(
        connect_timeout=3.05, read_timeout=10.0, slow_threshold=60
    )
    session.mount("https://", adapter)
    world = _world()
    provider = MagicMock()

    def get_artist(artist_id):
        session.post("https://provider.test/v1/browse", json={})
        return world[artist_id]

    provider.get_artist.side_effect = get_artist
    db = _five_favorites_db()
    _use(db, provider)

    response = _generate()

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    assert len(adapter.calls) == 2
    assert _replace_calls(db) == []


def test_generate_reads_colors_only_for_the_images_in_the_response(image_color_db):
    _use(_five_favorites_db(), _provider(_world(extras=True)))

    response = _generate()

    assert response.status_code == 200, response.text
    read = [key for keys in image_color_db.reads for key in keys]
    assert read
    assert not [k for k in read if "SONGIMG" in k or "SINGLEIMG" in k]
    # Every image of the response was asked for: the items of the lists, plus the
    # seeds' own images.
    assert any("Seed 1" in k for k in read)
    assert any("UCrel1x1" in k for k in read)


def _generated(db):
    _use(db, _provider(_world()))
    return lambda: _generate()


def test_generate_carries_the_stored_color(image_color_db, color_checks):
    db = _five_favorites_db()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_FAR_VALID_UNTIL)
    color_checks.carries(_generated(db))


def test_generate_reads_no_colors_when_there_is_nothing_to_read(
    image_color_db, color_checks
):
    _use(FakeRecommendationsDb(), _provider({}))

    color_checks.reads_nothing(_generate)


def test_generate_survives_a_failed_color_read(image_color_db, color_checks):
    db = _five_favorites_db()
    db.seed_rows(_USER_ID, generated_at=_OLD_GENERATED_AT, valid_until=_FAR_VALID_UNTIL)
    color_checks.survives_read_failure(_generated(db))


_EDITION_TITLES = {
    "MPREb_exp": "Whatever's Clever! (Expanded)",
    "MPREb_plain": "Whatever's Clever!",
    "MPREb_life": "A Wonderful Life",
    "MPREb_epi": "A Wonderful Life (Epilogue)",
}


def _album_lists(data):
    # Every album id of the response, list by list, plus the deprecated one.
    lists = {
        "albums_for_you": [a["id"] for a in data["albums_for_you"]],
        "seed_sections": [
            i["id"]
            for section in data["seed_sections"]
            for i in section["items"]
            if i["type"] == "album"
        ],
        "recommended_albums": [a["id"] for a in data["recommended_albums"]],
    }
    return lists, [a["id"] for a in data["albums"]]


def test_generate_keeps_one_edition_of_each_album_in_the_whole_response():
    world = _world()
    world["UCrel1x1"] = _artist_page(
        "Sim A",
        albums=["MPREb_exp", "MPREb_plain", "MPREb_a3"],
        titles=_EDITION_TITLES,
    )
    world["UCrel2x1"] = _artist_page(
        "Sim B",
        albums=["MPREb_life", "MPREb_b2", "MPREb_b3"],
        titles=_EDITION_TITLES,
    )
    world["UCrel3x1"] = _artist_page(
        "Sim C",
        albums=["MPREb_epi", "MPREb_c2", "MPREb_c3"],
        titles=_EDITION_TITLES,
    )
    _use(_five_favorites_db(), _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    lists, deprecated = _album_lists(data)
    everywhere = [i for ids in lists.values() for i in ids]
    # The one asked first in the order of assignment stays.
    assert everywhere.count("MPREb_exp") == 1
    assert "MPREb_plain" not in everywhere
    assert everywhere.count("MPREb_life") == 1
    assert "MPREb_epi" not in everywhere
    assert "MPREb_plain" not in deprecated
    assert "MPREb_epi" not in deprecated
    assert len(lists["albums_for_you"]) == 10
    assert len(lists["recommended_albums"]) == 10
    assert [
        len([i for i in s["items"] if i["type"] == "album"])
        for s in data["seed_sections"]
    ] == [3, 3, 3]
    assert len(deprecated) == len(set(deprecated)) == 29


def test_generate_never_recommends_a_soundtrack():
    titles = {
        "MPREb_beef": "BEEF Season 2 (Original Soundtrack)",
        "MPREb_jonas": "A Very Jonas Christmas Movie (Original Soundtrack)",
        "MPREb_ghost": "Ghost",
        "MPREb_st": "Stranger Things OST",
    }
    world = _world()
    world["UCrel1x1"] = _artist_page(
        "Sim A",
        albums=["MPREb_beef", "MPREb_jonas", "MPREb_ghost"],
        titles=titles,
    )
    albums = {
        "MPREbhist1": _album_page(
            "MPREbhist1",
            related=["MPREb_st", "MPREbrec1", "MPREbrec2"],
            titles=titles,
        )
    }
    db = _five_favorites_db(albums=["MPREbhist1"])
    _use(db, _provider(world, albums))

    response = _generate()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    lists, deprecated = _album_lists(data)
    everywhere = [i for ids in lists.values() for i in ids]
    assert not {"MPREb_beef", "MPREb_jonas", "MPREb_st"} & set(everywhere)
    assert not {"MPREb_beef", "MPREb_jonas", "MPREb_st"} & set(deprecated)
    assert everywhere.count("MPREb_ghost") == 1
    assert len(lists["albums_for_you"]) == 10
    assert len(lists["recommended_albums"]) == 10
    assert [
        len([i for i in s["items"] if i["type"] == "album"])
        for s in data["seed_sections"]
    ] == [3, 3, 3]


def test_generate_with_only_soundtracks_returns_empty_album_lists():
    world = _world()
    for page_id in list(world):
        if page_id in _SEEDS:
            continue
        ids = _albums_of(page_id)
        world[page_id] = _artist_page(
            f"Sim {page_id}",
            albums=ids,
            titles={a: f"{a} (Original Soundtrack)" for a in ids},
        )
    _use(_five_favorites_db(), _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["albums_for_you"] == []
    assert data["recommended_albums"] == []
    assert data["albums"] == []
    assert len(data["seed_sections"]) == 3
    for section in data["seed_sections"]:
        assert [i["type"] for i in section["items"]] == ["artist"] * 3
    assert len(data["recommended_artists"]) == 10


def _album_items(data):
    # Every album item of the response, from every list, plus the deprecated
    # one.
    items = [a for kind, a in _lists(data) if kind != "recommended_artists"]
    return [a for a in items if a.get("type", "album") == "album"] + data["albums"]


def test_generate_gives_an_album_without_artists_the_artist_of_its_page():
    ids = _albums_of("UCrel1x1")
    world = _world()
    world["UCrel1x1"] = _artist_page("Sim A", albums=ids, unlinked=ids)
    db = _five_favorites_db()
    _use(db, _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    items = _album_items(data)
    owned = [a for a in items if a["id"] in ids]
    assert owned
    for album in owned:
        assert album["artists"] == [{"id": "UCrel1x1", "name": "Sim A"}]
    others = [a for a in items if a["id"] not in ids]
    assert others
    for album in others:
        assert album["artists"] == [{"id": "UCsome", "name": "Some"}]
    # The row of the page has the measured shape: every key the provider
    # sends, and the artists key present with an empty list.
    page_row = world["UCrel1x1"]["albums"]["results"][0]
    assert sorted(page_row) == [
        "artists",
        "audioPlaylistId",
        "browseId",
        "isExplicit",
        "thumbnails",
        "title",
        "year",
    ]
    assert page_row["artists"] == []
    # No album of any of the four lists comes out without artists.
    assert items
    for album in items:
        assert album["artists"]
    rows = [r for r in _replace_calls(db)[0]["p_rows"] if r["item_type"] == "album"]
    assert rows
    for row in rows:
        assert row["payload"]["artists"]
    for row in rows:
        if row["item_id"] in ids:
            assert row["payload"]["artists"] == [{"id": "UCrel1x1", "name": "Sim A"}]


def test_generate_never_recommends_an_album_an_album_page_sends_without_artists():
    albums = {
        "MPREbhist1": _album_page(
            "MPREbhist1",
            related=["MPREb_bare", "MPREbrec1", "MPREbrec2"],
            unlinked=["MPREb_bare"],
        )
    }
    db = _five_favorites_db(albums=["MPREbhist1"])
    _use(db, _provider(_world(), albums))

    response = _generate()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    lists, deprecated = _album_lists(data)
    everywhere = [i for ids in lists.values() for i in ids]
    assert "MPREb_bare" not in everywhere
    assert "MPREb_bare" not in deprecated
    rec = next(i for i in _album_items(data) if i["id"] == "MPREbrec1")
    assert rec["reason"]["seed"]["type"] == "album"
    assert rec["reason"]["seed"]["id"] == "MPREbhist1"
    assert len(lists["albums_for_you"]) == 10
    assert len(lists["recommended_albums"]) == 10


def test_generate_keeps_one_edition_of_an_album_its_page_sends_without_artists():
    ids = ["MPREb_exp", "MPREb_plain", "MPREb_a3"]
    world = _world()
    world["UCrel1x1"] = _artist_page(
        "Sim A", albums=ids, titles=_EDITION_TITLES, unlinked=ids
    )
    _use(_five_favorites_db(), _provider(world))

    response = _generate()

    assert response.status_code == 200, response.text
    lists, deprecated = _album_lists(response.json()["data"])
    everywhere = [i for ids_ in lists.values() for i in ids_]
    assert everywhere.count("MPREb_exp") == 1
    assert "MPREb_plain" not in everywhere
    assert "MPREb_plain" not in deprecated
