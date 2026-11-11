# test/routes/test_ingest.py
#
# Tests for POST /ingest/genres and POST /ingest/releases, the internal
# endpoints the collector calls.
#
# Tested:
# - A valid body writes with exactly one rpc("replace_genre_playlists") whose
#   p_playlists / p_tracks are exact (sort_order 1..n per genre, position 1..n
#   per playlist, the seven keys of a tracks row, artists with an explicit id)
#   and answers 200 with the three counts
# - Without Authorization, with another scheme, with a wrong token, with a
#   user JWT, or with INGEST_TOKEN unset (any header, or none, and a log line
#   without values) the answer is 401 unauthorized and the fake database
#   saw nothing; a valid token reaches the service
# - The token is checked BEFORE the body is read: an invalid body, and a
#   malformed JSON, with no token or a wrong one are 401; with the right
#   token the malformed JSON is 422 invalid_request
# - An invalid, incomplete, incoherent or over-limit body (unknown key at each
#   level, empty lists, repeats, an id with no row, an orphan row, a primary
#   artist without id, a missing field, http thumbnail, ids outside the
#   pattern, each limit exceeded by one) is 422 invalid_request, with no
#   detail, and touches no table and calls no rpc; each limit at its exact
#   maximum is accepted
# - A slug that matches no genre is 404 genre_not_found and calls no rpc
# - A failing read of genres is 502 and calls no rpc; a failing rpc is 502; a
#   database timeout is 504; a count that is not the one sent (True included)
#   is 502
# - The same body twice sends the same parameters
# - Error bodies carry only ok and reason, and everything is no-store
# - The route uses get_ingest_db, not get_db
# - OpenAPI declares the request body of POST /ingest/genres (required, JSON,
#   with the model's fields and limits, no dangling "$ref")
#
# What is covered:
# - Happy path, rejection of what is empty or incomplete (an ingest has no
#   valid empty state: a playlist with no tracks is never written), parent
#   (genre) not found, upstream failure and timeout, the token and its order
#   against the body. The real function and queries are in
#   test/integration/test_ingest_integration.py
#
# - POST /ingest/releases (second half of the file): one rpc
#   ("replace_new_releases") with position 1..n in the order of the body and
#   200 with the count; the token checked before the body; each invalid body
#   (unknown keys, position or source in the body, repeats, patterns, limits,
#   forbidden artist names, http cover) is 422 and calls no rpc; each limit at
#   its maximum is accepted; an empty list is 422 (no valid empty submission);
#   rpc failure 502, timeout 504, a wrong count 502; same body twice, same
#   parameters; the route uses get_ingest_db; the request body is in OpenAPI
#
# Run with: pytest test/routes/test_ingest.py -v
#
# SEE: routes/ingest.py, services/ingest_service.py, models/ingest.py,
# core/auth.py

import copy
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError
from pydantic import SecretStr

from app import app
from core.config import settings
from core.database import get_db, get_ingest_db
from models.ingest import (
    MAX_ARTISTS_PER_RELEASE,
    MAX_GENRES,
    MAX_PLAYLISTS_PER_GENRE,
    MAX_RELEASES,
    MAX_TRACKS,
    MAX_TRACKS_PER_PLAYLIST,
)

client = TestClient(app, raise_server_exceptions=False)

_TOKEN = "a-fake-ingest-token-made-in-the-test-0123456789"
_NO_STORE = "no-store"
_URL = "/ingest/genres"


class _Query:
    def __init__(self, db: FakeDb, table: str):
        self.db = db
        self.table = table
        self.filters: list[tuple] = []

    def select(self, columns):
        self.columns = columns
        return self

    def in_(self, column, values):
        self.filters.append((column, list(values)))
        return self

    def execute(self):
        self.db.calls.append(("table", self.table))
        error = self.db.errors.get(self.table)
        if error is not None:
            raise error
        slugs = self.filters[0][1]
        return SimpleNamespace(
            data=[{"slug": s} for s in slugs if s not in self.db.unknown]
        )


class _Rpc:
    def __init__(self, db: FakeDb, name: str, params: dict):
        self.db = db
        self.name = name
        self.params = params

    def execute(self):
        self.db.calls.append(("rpc", self.name))
        self.db.rpcs.append((self.name, self.params))
        error = self.db.errors.get("rpc")
        if error is not None:
            raise error
        if self.db.returns is not None:
            return SimpleNamespace(data=self.db.returns)
        linked = sum(len(p["tracks"]) for p in self.params["p_playlists"])
        return SimpleNamespace(data=linked)


class FakeDb:
    # Imitates the postgrest chain the service uses: table("genres").select()
    # .in_().execute() and rpc(name, params).execute(). unknown are the slugs
    # the genres table does not have; errors maps "genres" or "rpc" to the
    # exception its execute() raises; returns overrides what the rpc answers.
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.rpcs: list[tuple[str, dict]] = []
        self.unknown: set[str] = set()
        self.errors: dict[str, Exception] = {}
        self.returns = None

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        return _Rpc(self, name, params)


def _track_row(track_id):
    return {
        "track_id": track_id,
        "title": f"Title {track_id}",
        "artists": [{"id": "UCartist", "name": "Artist"}, {"name": "Guest"}],
        "album": "Album",
        "album_id": "MPREb_album",
        "duration_seconds": 215,
        "thumbnail_url": "https://example.com/cover.jpg",
    }


def _body(layout=None):
    # layout: {slug: [[track ids of playlist 1], [track ids of playlist 2]]}
    layout = layout or {"rock": [["t1", "t2"], ["t2", "t3"]], "pop": [["t4"]]}
    genres = []
    ids: list[str] = []
    for slug, playlists in layout.items():
        genres.append(
            {
                "slug": slug,
                "playlists": [
                    {
                        "source_playlist_id": f"PL{slug}{index}",
                        "title": f"Playlist {slug} {index}",
                        "description": None,
                        "category": None,
                        "tracks": list(tracks),
                    }
                    for index, tracks in enumerate(playlists, start=1)
                ],
            }
        )
        for tracks in playlists:
            for track_id in tracks:
                if track_id not in ids:
                    ids.append(track_id)
    return {"genres": genres, "tracks": [_track_row(i) for i in ids]}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "ingest_token", SecretStr(_TOKEN))


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_ingest_db, None)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def db():
    fake = FakeDb()
    app.dependency_overrides[get_ingest_db] = lambda: fake
    return fake


def _post(body, token=_TOKEN, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post(_URL, json=body, headers=headers, **kwargs)


# --- With data -------------------------------------------------------------


def test_valid_body_writes_with_one_rpc_and_returns_the_counts(db):
    response = _post(_body())

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {"genres": 2, "playlists": 3, "tracks": 5},
    }
    assert len(db.rpcs) == 1
    name, params = db.rpcs[0]
    assert name == "replace_genre_playlists"
    assert set(params) == {"p_playlists", "p_tracks"}
    assert params["p_playlists"] == [
        {
            "genre_slug": "rock",
            "source_playlist_id": "PLrock1",
            "title": "Playlist rock 1",
            "description": None,
            "category": None,
            "sort_order": 1,
            "tracks": [
                {"track_id": "t1", "position": 1},
                {"track_id": "t2", "position": 2},
            ],
        },
        {
            "genre_slug": "rock",
            "source_playlist_id": "PLrock2",
            "title": "Playlist rock 2",
            "description": None,
            "category": None,
            "sort_order": 2,
            "tracks": [
                {"track_id": "t2", "position": 1},
                {"track_id": "t3", "position": 2},
            ],
        },
        {
            "genre_slug": "pop",
            "source_playlist_id": "PLpop1",
            "title": "Playlist pop 1",
            "description": None,
            "category": None,
            "sort_order": 1,
            "tracks": [{"track_id": "t4", "position": 1}],
        },
    ]
    assert [t["track_id"] for t in params["p_tracks"]] == ["t1", "t2", "t3", "t4"]
    for row in params["p_tracks"]:
        assert set(row) == {
            "track_id",
            "title",
            "artists",
            "album",
            "album_id",
            "duration_seconds",
            "thumbnail_url",
        }
        assert row["artists"] == [
            {"id": "UCartist", "name": "Artist"},
            {"id": None, "name": "Guest"},
        ]
    assert ("table", "genres") in db.calls


def test_description_and_category_are_sent_when_given(db):
    body = _body({"rock": [["t1"]]})
    playlist = body["genres"][0]["playlists"][0]
    playlist["description"] = "A description"
    playlist["category"] = "Classics"

    assert _post(body).status_code == 200

    sent = db.rpcs[0][1]["p_playlists"][0]
    assert sent["description"] == "A description"
    assert sent["category"] == "Classics"


def test_same_body_twice_sends_the_same_rpc_params(db):
    body = _body()

    assert _post(body).status_code == 200
    assert _post(body).status_code == 200

    assert len(db.rpcs) == 2
    assert db.rpcs[0] == db.rpcs[1]


# --- The token -------------------------------------------------------------


def _assert_unauthorized(response, db):
    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    assert response.headers["cache-control"] == _NO_STORE
    assert db.calls == []


def test_missing_authorization_is_401(db):
    _assert_unauthorized(_post(_body(), token=None), db)


def test_wrong_token_is_401(db):
    _assert_unauthorized(_post(_body(), token=_TOKEN + "x"), db)


def test_a_prefix_of_the_token_is_401(db):
    _assert_unauthorized(_post(_body(), token=_TOKEN[:-1]), db)


def test_non_bearer_scheme_is_401(db):
    response = client.post(_URL, json=_body(), headers={"Authorization": _TOKEN})
    _assert_unauthorized(response, db)
    response = client.post(
        _URL, json=_body(), headers={"Authorization": f"Basic {_TOKEN}"}
    )
    _assert_unauthorized(response, db)


def test_a_user_jwt_is_401(db):
    jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    _assert_unauthorized(_post(_body(), token=jwt_like), db)


@pytest.mark.parametrize("header", [_TOKEN, "", None])
def test_unset_ingest_token_is_401_for_any_header_and_logs_not_configured(
    db, monkeypatch, caplog, header
):
    monkeypatch.setattr(settings, "ingest_token", None)

    with caplog.at_level(logging.WARNING):
        response = _post(_body(), token=header)

    _assert_unauthorized(response, db)
    messages = [r.getMessage() for r in caplog.records]
    assert "ingest_token_not_configured" in messages
    assert all(_TOKEN not in m for m in messages)


def test_valid_token_reaches_the_service(db):
    assert _post(_body()).status_code == 200
    assert db.calls != []


def test_401_wins_over_an_invalid_body(db):
    body = _body()
    body["unexpected"] = True
    body["genres"] = []

    _assert_unauthorized(_post(body, token=None), db)
    _assert_unauthorized(_post(body, token="wrong"), db)


_MALFORMED = b'{"genres": [ {"slug": '


def _post_raw(content, token):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(_URL, content=content, headers=headers)


def test_malformed_json_without_token_is_401(db):
    _assert_unauthorized(_post_raw(_MALFORMED, None), db)


def test_malformed_json_with_a_wrong_token_is_401(db):
    _assert_unauthorized(_post_raw(_MALFORMED, "wrong"), db)


@pytest.mark.parametrize("content", [_MALFORMED, b"", b"\xff\xfe", b"[]", b"null"])
def test_malformed_json_with_the_right_token_is_422(db, content):
    response = _post_raw(content, _TOKEN)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert response.headers["cache-control"] == _NO_STORE
    assert db.calls == []


# --- Invalid bodies --------------------------------------------------------


def _set(path, value):
    def mutate(body):
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _delete(path):
    def mutate(body):
        target = body
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _first_playlist(body):
    return body["genres"][0]["playlists"][0]


def _orphan_row(body):
    body["tracks"].append(_track_row("orphan"))


def _reference_without_row(body):
    _first_playlist(body)["tracks"].append("no-row")


def _row_without_id_in_artists(body):
    body["tracks"][0]["artists"] = [{"name": "No id"}, {"id": "UCx", "name": "X"}]


_INVALID = {
    "unknown key at the root": _set(["extra"], 1),
    "unknown key in a genre": _set(["genres", 0, "extra"], 1),
    "unknown key in a playlist": _set(["genres", 0, "playlists", 0, "extra"], 1),
    "unknown key in a track row": _set(["tracks", 0, "extra"], 1),
    "no genres": _set(["genres"], []),
    "no tracks": _set(["tracks"], []),
    "genres missing": _delete(["genres"]),
    "tracks missing": _delete(["tracks"]),
    "genre with no playlists": _set(["genres", 0, "playlists"], []),
    "playlist with no tracks": _set(["genres", 0, "playlists", 0, "tracks"], []),
    "repeated slug": lambda b: b["genres"].append(copy.deepcopy(b["genres"][0])),
    "repeated source in a genre": lambda b: b["genres"][0]["playlists"].append(
        copy.deepcopy(b["genres"][0]["playlists"][0])
    ),
    "repeated track in a playlist": _set(
        ["genres", 0, "playlists", 0, "tracks"], ["t1", "t1", "t2"]
    ),
    "repeated row in tracks": lambda b: b["tracks"].append(
        copy.deepcopy(b["tracks"][0])
    ),
    "referenced id with no row": _reference_without_row,
    "orphan row": _orphan_row,
    "primary artist without id": _row_without_id_in_artists,
    "primary artist with empty id": _set(
        ["tracks", 0, "artists"], [{"id": "", "name": "A"}]
    ),
    "empty artists": _set(["tracks", 0, "artists"], []),
    "too many artists": _set(
        ["tracks", 0, "artists"], [{"id": f"UC{i}", "name": "A"} for i in range(21)]
    ),
    "album missing": _delete(["tracks", 0, "album"]),
    "album_id missing": _delete(["tracks", 0, "album_id"]),
    "duration missing": _delete(["tracks", 0, "duration_seconds"]),
    "thumbnail missing": _delete(["tracks", 0, "thumbnail_url"]),
    "http thumbnail": _set(["tracks", 0, "thumbnail_url"], "http://example.com/a.jpg"),
    "duration zero": _set(["tracks", 0, "duration_seconds"], 0),
    "duration over a day": _set(["tracks", 0, "duration_seconds"], 86_401),
    "empty title": _set(["tracks", 0, "title"], ""),
    "track id outside the pattern": _set(["tracks", 0, "track_id"], "a b"),
    "source id outside the pattern": _set(
        ["genres", 0, "playlists", 0, "source_playlist_id"], "PL;drop"
    ),
    "slug with a comma": _set(["genres", 0, "slug"], "a,b"),
    "slug with a space": _set(["genres", 0, "slug"], "a b"),
    "empty slug": _set(["genres", 0, "slug"], ""),
    "empty playlist title": _set(["genres", 0, "playlists", 0, "title"], ""),
    "description too long": _set(
        ["genres", 0, "playlists", 0, "description"], "x" * 2001
    ),
    "category too long": _set(["genres", 0, "playlists", 0, "category"], "x" * 101),
    "empty category": _set(["genres", 0, "playlists", 0, "category"], ""),
}


@pytest.mark.parametrize("name", list(_INVALID))
def test_invalid_body_is_422_and_touches_no_table(db, name):
    body = _body({"rock": [["t1", "t2"]], "pop": [["t3"]]})
    _INVALID[name](body)

    response = _post(body)

    assert response.status_code == 422, name
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert db.calls == []


def _distinct_layout(genres, playlists, per_playlist):
    layout = {}
    counter = 0
    for g in range(genres):
        lists = []
        for _ in range(playlists):
            lists.append([f"t{counter + i}" for i in range(per_playlist)])
            counter += per_playlist
        layout[f"g{g}"] = lists
    return layout


_LIMITS = {
    "genres": (MAX_GENRES, lambda n: _distinct_layout(n, 1, 1)),
    "playlists per genre": (
        MAX_PLAYLISTS_PER_GENRE,
        lambda n: _distinct_layout(1, n, 1),
    ),
    "tracks per playlist": (
        MAX_TRACKS_PER_PLAYLIST,
        lambda n: _distinct_layout(1, 1, n),
    ),
}


@pytest.mark.parametrize("name", list(_LIMITS))
def test_each_limit_over_by_one_is_422(db, name):
    limit, build = _LIMITS[name]

    response = _post(_body(build(limit + 1)))

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert db.calls == []


def test_more_than_the_maximum_distinct_tracks_is_422(db):
    # 34 playlists of 300 tracks over two genres: 10 200 distinct tracks.
    body = _body(_distinct_layout(2, 17, MAX_TRACKS_PER_PLAYLIST))
    assert len(body["tracks"]) > MAX_TRACKS

    response = _post(body)

    assert response.status_code == 422
    assert db.calls == []


@pytest.mark.parametrize("name", list(_LIMITS))
def test_limits_at_the_maximum_are_accepted(db, name):
    limit, build = _LIMITS[name]

    response = _post(_body(build(limit)))

    assert response.status_code == 200


def test_the_maximum_of_distinct_tracks_is_accepted(db):
    # 33 playlists of 300 plus one of 100: exactly 10 000 distinct tracks.
    lists = []
    counter = 0
    for size in [MAX_TRACKS_PER_PLAYLIST] * 33 + [100]:
        lists.append([f"t{counter + i}" for i in range(size)])
        counter += size
    layout = {"g0": lists[:17], "g1": lists[17:]}
    body = _body(layout)
    assert len(body["tracks"]) == MAX_TRACKS

    response = _post(body)

    assert response.status_code == 200
    assert response.json()["data"]["tracks"] == MAX_TRACKS


# --- Parent not found and upstream -----------------------------------------


def test_unknown_slug_is_404_genre_not_found_and_calls_no_rpc(db, caplog):
    db.unknown = {"pop"}

    with caplog.at_level(logging.WARNING):
        response = _post(_body())

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "genre_not_found"}
    assert db.rpcs == []
    assert all("pop" not in r.getMessage() for r in caplog.records)


def test_genres_read_failure_is_502_and_calls_no_rpc(db):
    db.errors["genres"] = APIError({"message": "boom"})

    response = _post(_body())

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert db.rpcs == []


def test_rpc_failure_is_502(db):
    db.errors["rpc"] = APIError({"message": "boom", "code": "23505"})

    response = _post(_body())

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_rpc_timeout_is_504(db):
    db.errors["rpc"] = httpx.ReadTimeout("slow")

    response = _post(_body())

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


@pytest.mark.parametrize("returned", [4, 0, 6, True, "5", [5]])
def test_rpc_count_mismatch_is_502(db, returned):
    db.returns = returned

    response = _post(_body())

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_error_bodies_carry_no_details_and_no_store(db):
    db.errors["rpc"] = APIError({"message": "secret internal detail"})
    cases = [
        _post(_body()),
        _post(_body(), token=None),
        _post({"unexpected": 1}),
    ]
    db.errors["rpc"] = httpx.ReadTimeout("secret internal detail")
    cases.append(_post(_body()))
    db.unknown = {"rock"}
    cases.append(_post(_body()))

    assert [r.status_code for r in cases] == [502, 401, 422, 504, 404]
    for response in cases:
        assert set(response.json()) == {"ok", "reason"}
        assert "secret" not in response.text
        assert response.headers["cache-control"] == _NO_STORE


def test_route_uses_the_ingest_client(db):
    class _Exploding:
        def table(self, name):
            raise AssertionError("the route used get_db")

        def rpc(self, name, params):
            raise AssertionError("the route used get_db")

    app.dependency_overrides[get_db] = lambda: _Exploding()

    assert _post(_body()).status_code == 200


def test_openapi_declares_the_request_body():
    operation = app.openapi()["paths"]["/ingest/genres"]["post"]

    body = operation["requestBody"]
    assert body["required"] is True
    schema = body["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"genres", "tracks"}
    assert schema["properties"]["genres"]["maxItems"] == MAX_GENRES
    playlist = schema["properties"]["genres"]["items"]["properties"]["playlists"]
    assert playlist["maxItems"] == MAX_PLAYLISTS_PER_GENRE
    assert "$defs" not in schema
    assert "#/$defs" not in json.dumps(schema)


# --- POST /ingest/releases ---------------------------------------------------

_RELEASES_URL = "/ingest/releases"


def _release(n=1, **overrides):
    return {
        "album_id": f"MPREb_{n:011d}",
        "audio_playlist_id": f"OLAK5uy_{n:033d}",
        "type": "album",
        "title": f"Release {n}",
        "artists": [{"id": f"UC{n:022d}", "name": f"Artist {n}"}],
        "thumbnail_url": f"https://example.com/cover{n}.jpg",
        **overrides,
    }


def _release_body(n=2):
    return {"releases": [_release(i) for i in range(1, n + 1)]}


class _ReleaseRpc:
    def __init__(self, db: _ReleaseDb, name: str, params: dict):
        self.db = db
        self.name = name
        self.params = params

    def execute(self):
        self.db.calls.append(("rpc", self.name))
        self.db.rpcs.append((self.name, self.params))
        if self.db.error is not None:
            raise self.db.error
        if self.db.returns is not None:
            return SimpleNamespace(data=self.db.returns)
        return SimpleNamespace(data=len(self.params["p_releases"]))


class _ReleaseDb:
    # rpc(name, params).execute(); error is raised by execute() and returns
    # overrides what the rpc answers. There is no table access to imitate.
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.rpcs: list[tuple[str, dict]] = []
        self.error: Exception | None = None
        self.returns = None

    def rpc(self, name, params):
        return _ReleaseRpc(self, name, params)

    def table(self, name):
        raise AssertionError("a release ingest reads no table")


@pytest.fixture
def release_db():
    fake = _ReleaseDb()
    app.dependency_overrides[get_ingest_db] = lambda: fake
    return fake


def _post_releases(body, token=_TOKEN, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post(_RELEASES_URL, json=body, headers=headers, **kwargs)


def _post_releases_raw(content, token):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(_RELEASES_URL, content=content, headers=headers)


def _assert_release_unauthorized(response, db):
    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    assert response.headers["cache-control"] == _NO_STORE
    assert db.calls == []


def test_valid_release_body_writes_with_one_rpc_and_returns_the_count(release_db):
    response = _post_releases(_release_body(3))

    assert response.status_code == 200
    assert response.headers["cache-control"] == _NO_STORE
    assert response.json() == {"ok": True, "data": {"releases": 3}}
    assert release_db.calls == [("rpc", "replace_new_releases")]
    (name, params) = release_db.rpcs[0]
    assert name == "replace_new_releases"
    assert params == {
        "p_releases": [{**_release(i), "position": i} for i in range(1, 4)]
    }


def test_release_same_body_twice_sends_the_same_rpc_params(release_db):
    _post_releases(_release_body(3))
    _post_releases(_release_body(3))

    assert release_db.rpcs[0] == release_db.rpcs[1]


@pytest.mark.parametrize("header", ["none", "basic", "wrong", "prefix", "jwt"])
def test_release_auth_failures_are_401_and_touch_nothing(release_db, header):
    headers = {
        "none": {},
        "basic": {"Authorization": f"Basic {_TOKEN}"},
        "wrong": {"Authorization": f"Bearer {_TOKEN}x"},
        "prefix": {"Authorization": f"Bearer {_TOKEN[:-1]}"},
        "jwt": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"},
    }[header]

    response = client.post(_RELEASES_URL, json=_release_body(), headers=headers)

    _assert_release_unauthorized(response, release_db)


def test_release_unset_ingest_token_is_401(release_db, monkeypatch):
    monkeypatch.setattr(settings, "ingest_token", None)

    _assert_release_unauthorized(_post_releases(_release_body()), release_db)


def test_release_malformed_json_without_token_is_401(release_db):
    _assert_release_unauthorized(
        _post_releases_raw(b'{"releases": [ {"album_id": ', None), release_db
    )


def test_release_401_wins_over_an_invalid_body(release_db):
    body = {"unexpected": True, "releases": []}

    _assert_release_unauthorized(_post_releases(body, token=None), release_db)
    _assert_release_unauthorized(_post_releases(body, token="wrong"), release_db)


@pytest.mark.parametrize(
    "content", [b'{"releases": [ {"album_id": ', b"", b"\xff\xfe", b"[]", b"null"]
)
def test_release_malformed_json_with_the_right_token_is_422(release_db, content):
    response = _post_releases_raw(content, _TOKEN)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert response.headers["cache-control"] == _NO_STORE
    assert release_db.calls == []


def _with_release(**overrides):
    return {"releases": [_release(1, **overrides), _release(2)]}


def _with_artist(**overrides):
    artist = {"id": f"UC{1:022d}", "name": "Artist", **overrides}
    return _with_release(artists=[artist])


_INVALID_RELEASE_BODIES = {
    "unknown_root_key": lambda: {**_release_body(), "extra": 1},
    "unknown_release_key": lambda: _with_release(extra=1),
    "unknown_artist_key": lambda: _with_artist(extra=1),
    "position_in_the_body": lambda: _with_release(position=1),
    "source_in_the_body": lambda: _with_release(source="provider_new_releases"),
    "empty_list": lambda: {"releases": []},
    "missing_releases": dict,
    "repeated_album": lambda: {
        "releases": [_release(1), _release(2, album_id=_release(1)["album_id"])]
    },
    "missing_field": lambda: {
        "releases": [{k: v for k, v in _release(1).items() if k != "title"}]
    },
    "album_id_pattern": lambda: _with_release(album_id="XXXXb_123"),
    "album_id_129": lambda: _with_release(album_id="MPREb_" + "a" * 123),
    "playlist_pattern": lambda: _with_release(audio_playlist_id="PL123"),
    "playlist_129": lambda: _with_release(audio_playlist_id="OLAK5uy_" + "a" * 121),
    "artist_id_21": lambda: _with_artist(id="UC" + "a" * 21),
    "artist_id_23": lambda: _with_artist(id="UC" + "a" * 23),
    "artist_id_without_uc": lambda: _with_artist(id="XX" + "a" * 22),
    "type_not_normalized": lambda: _with_release(type="Album"),
    "type_song": lambda: _with_release(type="song"),
    "title_empty": lambda: _with_release(title=""),
    "title_301": lambda: _with_release(title="t" * 301),
    "name_empty": lambda: _with_artist(name=""),
    "name_blank": lambda: _with_artist(name="  "),
    "name_301": lambda: _with_artist(name="n" * 301),
    "name_null_upper": lambda: _with_artist(name="NULL"),
    "name_none_padded": lambda: _with_artist(name=" none "),
    "name_undefined": lambda: _with_artist(name="Undefined"),
    "no_artists": lambda: _with_release(artists=[]),
    "21_artists": lambda: _with_release(
        artists=[
            {"id": f"UC{i:022d}", "name": f"Artist {i}"}
            for i in range(1, MAX_ARTISTS_PER_RELEASE + 2)
        ]
    ),
    "cover_http": lambda: _with_release(thumbnail_url="http://example.com/c.jpg"),
    "cover_2049": lambda: _with_release(
        thumbnail_url="https://example.com/"
        + "a" * (2049 - len("https://example.com/"))
    ),
    "501_releases": lambda: {
        "releases": [_release(i) for i in range(1, MAX_RELEASES + 2)]
    },
}


@pytest.mark.parametrize("name", list(_INVALID_RELEASE_BODIES))
def test_invalid_release_body_is_422_and_calls_no_rpc(release_db, name):
    response = _post_releases(_INVALID_RELEASE_BODIES[name]())

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert response.headers["cache-control"] == _NO_STORE
    assert release_db.calls == []


def test_release_limits_at_the_maximum_are_accepted(release_db):
    cover = "https://example.com/" + "a" * (2048 - len("https://example.com/"))
    first = _release(
        1,
        title="t" * 300,
        thumbnail_url=cover,
        artists=[
            {"id": f"UC{i:022d}", "name": "n" * 300}
            for i in range(1, MAX_ARTISTS_PER_RELEASE + 1)
        ],
    )
    body = {"releases": [first, *[_release(i) for i in range(2, MAX_RELEASES + 1)]]}

    response = _post_releases(body)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"releases": MAX_RELEASES}}


def test_release_rpc_failure_is_502(release_db):
    release_db.error = APIError({"message": "boom", "code": "23505"})

    response = _post_releases(_release_body())

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_release_rpc_timeout_is_504(release_db):
    release_db.error = httpx.ReadTimeout("slow")

    response = _post_releases(_release_body())

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


@pytest.mark.parametrize("returned", [1, 0, 3, True, "2", [2]])
def test_release_rpc_count_mismatch_is_502(release_db, returned):
    release_db.returns = returned

    response = _post_releases(_release_body(2))

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_release_error_bodies_carry_no_details_and_no_store(release_db):
    release_db.error = APIError({"message": "secret internal detail"})
    cases = [
        _post_releases(_release_body()),
        _post_releases(_release_body(), token=None),
        _post_releases({"unexpected": 1}),
    ]
    release_db.error = httpx.ReadTimeout("secret internal detail")
    cases.append(_post_releases(_release_body()))

    assert [r.status_code for r in cases] == [502, 401, 422, 504]
    for response in cases:
        assert set(response.json()) == {"ok", "reason"}
        assert "secret" not in response.text
        assert response.headers["cache-control"] == _NO_STORE


def test_release_route_uses_the_ingest_client(release_db):
    class _Exploding:
        def table(self, name):
            raise AssertionError("the route used get_db")

        def rpc(self, name, params):
            raise AssertionError("the route used get_db")

    app.dependency_overrides[get_db] = lambda: _Exploding()

    assert _post_releases(_release_body()).status_code == 200


def test_openapi_declares_the_release_request_body():
    operation = app.openapi()["paths"]["/ingest/releases"]["post"]

    body = operation["requestBody"]
    assert body["required"] is True
    schema = body["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"releases"}
    assert schema["properties"]["releases"]["maxItems"] == MAX_RELEASES
    assert "$defs" not in schema
    assert "#/$defs" not in json.dumps(schema)
