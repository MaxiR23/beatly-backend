# test/routes/test_releases.py
#
# Tests for GET /releases, the public list of new releases.
#
# Tested:
# - GET returns the stored releases ordered by position (album_id, title, type,
#   artists with an id string, the cover rewritten to 544 with smart crop and
#   the image tiers; never position nor audio_playlist_id)
# - GET with an empty table returns 200 with an empty first page
# - GET paginates: first page with has_more and total, second page with the
#   returned cursor and a null total, last page without a cursor; an invalid
#   cursor is 422 invalid_cursor without touching the database; limit 0 and 101
#   are 422 invalid_request
# - The query reads the new_releases table filtered by the source of the
#   ingest, ordered by position and then album_id, one row over the limit,
#   with the service-role client (get_db); the rows of another source are not
#   served
# - GET database failure returns 502, a database timeout 504, and a stored
#   row that breaks the model (a type outside the set, an artist without id, a
#   missing cover) returns 502
# - GET needs no token; every answer sends no-store
# - GET carries the stored color of its images and survives a failed color read
#
# What is covered:
# - Happy path, expected empty state, pagination, upstream failure and
#   timeout. There is no parent resource. The real query and the table are in
#   test/integration/test_releases_integration.py
#
# Database access is overridden through get_db (core/database.py).
#
# Run with: pytest test/routes/test_releases.py -v
#
# SEE: routes/releases.py, services/release_service.py, models/releases.py

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core.database import get_db

client = TestClient(app, raise_server_exceptions=False)

_NO_STORE = "no-store"
_SOURCE = "provider_new_releases"
_URL = "/releases"


def _thumb(name: str) -> str:
    return f"https://lh3.googleusercontent.com/{name}=w544-h544-l90-rj"


def _rows(count=5):
    return [
        {
            "source": _SOURCE,
            "album_id": f"MPREb_{i}",
            "audio_playlist_id": f"OLAK5uy_{i}",
            "title": f"Release {i}",
            "type": "album",
            "artists": [{"id": "UC" + f"{i:022d}", "name": f"Artist {i}"}],
            "thumbnail_url": _thumb(f"rel{i}"),
            "position": i,
        }
        for i in range(1, count + 1)
    ]


class _Query:
    def __init__(self, db: FakeDb):
        self.db = db
        self.columns = None
        self.count = None
        self.filters: list[tuple] = []
        self.orders: list[tuple] = []
        self.max_rows: int | None = None

    def select(self, columns, count=None):
        self.columns = columns
        self.count = count
        return self

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def or_(self, expression):
        self.filters.append(("or", expression))
        return self

    def order(self, column, desc=False):
        self.orders.append((column, desc))
        return self

    def limit(self, count):
        self.max_rows = count
        return self

    def execute(self):
        self.db.queries.append(self)
        if self.db.error is not None:
            raise self.db.error
        rows = sorted(self.db.rows, key=lambda r: r["position"])
        for kind, *rest in self.filters:
            if kind == "eq":
                rows = [r for r in rows if r[rest[0]] == rest[1]]
            else:
                # keyset_filter: "position.gt.N,and(...)".
                after = int(rest[0].split(",")[0].rsplit(".", 1)[1])
                rows = [r for r in rows if r["position"] > after]
        total = len([r for r in self.db.rows if r["source"] == _SOURCE])
        if self.max_rows is not None:
            rows = rows[: self.max_rows]
        wanted = [c.strip() for c in self.columns.split(",")]
        data = [{c: r[c] for c in wanted if c in r} for r in rows]
        return SimpleNamespace(data=data, count=total if self.count else None)


class FakeDb:
    # Imitates table("new_releases").select().eq().order().limit().execute().
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.error: Exception | None = None
        self.queries: list[_Query] = []

    def table(self, name):
        assert name == "new_releases"
        return _Query(self)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_db, None)


def _use(db):
    app.dependency_overrides[get_db] = lambda: db
    return db


def test_get_releases_returns_the_items_in_position_order():
    _use(FakeDb(list(reversed(_rows()))))

    response = client.get(_URL)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == _NO_STORE
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert [i["album_id"] for i in data["items"]] == [f"MPREb_{i}" for i in range(1, 6)]
    assert data["items"][0] == {
        "album_id": "MPREb_1",
        "title": "Release 1",
        "type": "album",
        "artists": [{"id": "UC" + f"{1:022d}", "name": "Artist 1"}],
        "thumbnail_url": "https://lh3.googleusercontent.com/rel1=w544-h544-p-l90-rj",
        "image": {
            "small": "https://lh3.googleusercontent.com/rel1=w192-h192-p-l90-rj",
            "medium": "https://lh3.googleusercontent.com/rel1=w600-h600-p-l90-rj",
            "large": "https://lh3.googleusercontent.com/rel1=w1320-h1320-p-l90-rj",
            "color": None,
        },
    }
    assert data["page"] == {
        "limit": 50,
        "next_cursor": None,
        "has_more": False,
        "total": 5,
    }


def test_get_releases_without_rows_returns_an_empty_first_page():
    _use(FakeDb())

    response = client.get(_URL)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [],
            "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 0},
        },
    }


def test_get_releases_first_page_has_more_and_total():
    _use(FakeDb(_rows(5)))

    response = client.get(f"{_URL}?limit=2")

    data = response.json()["data"]
    assert [i["album_id"] for i in data["items"]] == ["MPREb_1", "MPREb_2"]
    assert data["page"]["has_more"] is True
    assert data["page"]["total"] == 5
    assert data["page"]["next_cursor"]


def test_get_releases_second_page_with_the_cursor_has_null_total():
    _use(FakeDb(_rows(5)))
    cursor = client.get(f"{_URL}?limit=2").json()["data"]["page"]["next_cursor"]

    response = client.get(f"{_URL}?limit=2&cursor={cursor}")

    data = response.json()["data"]
    assert [i["album_id"] for i in data["items"]] == ["MPREb_3", "MPREb_4"]
    assert data["page"]["total"] is None
    assert data["page"]["has_more"] is True


def test_get_releases_last_page_has_no_cursor():
    _use(FakeDb(_rows(5)))
    cursor = client.get(f"{_URL}?limit=4").json()["data"]["page"]["next_cursor"]

    response = client.get(f"{_URL}?limit=4&cursor={cursor}")

    data = response.json()["data"]
    assert [i["album_id"] for i in data["items"]] == ["MPREb_5"]
    assert data["page"]["has_more"] is False
    assert data["page"]["next_cursor"] is None


def test_get_releases_invalid_cursor_is_422_without_touching_the_database():
    db = _use(FakeDb(_rows()))

    response = client.get(f"{_URL}?cursor=garbage")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    assert response.headers["Cache-Control"] == _NO_STORE
    assert db.queries == []


@pytest.mark.parametrize("limit", [0, 101])
def test_get_releases_invalid_limit_is_invalid_request(limit):
    db = _use(FakeDb())

    response = client.get(f"{_URL}?limit={limit}")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert db.queries == []


def test_get_releases_queries_the_source_ordered_by_position_then_album_id():
    other = {**_rows(1)[0], "source": "other", "album_id": "MPREb_other"}
    db = _use(FakeDb([*_rows(2), other]))

    response = client.get(_URL)

    (query,) = db.queries
    assert query.columns == "album_id, title, type, artists, thumbnail_url, position"
    assert ("eq", "source", _SOURCE) in query.filters
    assert query.orders == [("position", False), ("album_id", False)]
    assert query.max_rows == 51
    assert [i["album_id"] for i in response.json()["data"]["items"]] == [
        "MPREb_1",
        "MPREb_2",
    ]


def test_get_releases_reads_with_the_service_role_client():
    # get_db is the only client the route asks for: the table has no
    # privileges for the client roles, so the answer proves it was this one.
    db = _use(FakeDb(_rows(1)))

    assert client.get(_URL).status_code == 200
    assert len(db.queries) == 1


def test_get_releases_needs_no_token():
    _use(FakeDb(_rows(1)))

    response = client.get(_URL)

    assert response.status_code == 200


def test_get_releases_sends_no_store():
    _use(FakeDb(_rows(1)))
    ok = client.get(_URL)
    db = _use(FakeDb())
    db.error = APIError({"message": "boom"})
    failed = client.get(_URL)

    assert ok.headers["Cache-Control"] == _NO_STORE
    assert failed.headers["Cache-Control"] == _NO_STORE


def test_get_releases_database_failure_returns_upstream_error():
    db = _use(FakeDb(_rows()))
    db.error = APIError({"message": "secret internal detail"})

    response = client.get(_URL)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_releases_database_timeout_returns_upstream_timeout():
    db = _use(FakeDb(_rows()))
    db.error = httpx.ReadTimeout("slow")

    response = client.get(_URL)

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


@pytest.mark.parametrize(
    "change",
    [
        {"type": "song"},
        {"artists": [{"id": None, "name": "Artist"}]},
        {"thumbnail_url": None},
    ],
    ids=["type_outside_the_set", "artist_without_id", "missing_cover"],
)
def test_get_releases_stored_row_that_breaks_the_model_is_upstream_error(change):
    _use(FakeDb([{**_rows(1)[0], **change}]))

    response = client.get(_URL)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_get_releases_carries_the_stored_color(image_color_db, color_checks):
    _use(FakeDb(_rows()))

    color_checks.carries(lambda: client.get(_URL))


def test_get_releases_survives_a_failed_color_read(image_color_db, color_checks):
    _use(FakeDb(_rows()))

    color_checks.survives_read_failure(lambda: client.get(_URL))
