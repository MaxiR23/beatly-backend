# test/routes/test_onboarding.py
#
# Tests for the onboarding endpoints.
#
# Tested:
# - GET /onboarding/artists returns the curated artists ordered by position
#   (id, name, thumbnail_url and the image tiers, never the position), read
#   with the service-role client (get_db)
# - GET with an empty table returns 200 with an empty first page
# - GET paginates: first page with has_more and total, second page with the
#   returned cursor and a null total, last page without a cursor, and an
#   invalid cursor is 422 invalid_cursor without touching the database
# - GET database failure returns 502, a database timeout 504
# - GET and POST without a token return 401; both send private, no-cache
# - GET carries the stored color of its images
# - POST /onboarding/complete with curated ids saves them in the library
#   (kind artist, source external, title and thumbnail of the table) as the
#   user, marks the profile as service-role filtered by the id of the token,
#   and calls no provider
# - POST with ids outside the table calls the provider only for those, in
#   parallel; a missing artist is skipped and logged; the rest is saved
# - POST with fewer than 5 valid artists left is 422 too_few_artists and
#   writes nothing
# - POST on a profile already completed keeps the first timestamp, writes
#   nothing to profiles and upserts again without duplicating
# - POST when another request marked the profile in between rereads the
#   timestamp
# - POST with fewer than 5 ids, more than 20, repeated ids, a malformed id,
#   an id that would break the in_ filter, a missing or empty list, or an
#   extra key (user_id) is 422 invalid_request and touches nothing
# - POST with no profile is 404 profile_not_found and writes nothing
# - POST provider failure, timeout, a KeyError with an existing page, a
#   failing probe and an exhausted request budget return 502 or 504 and
#   write nothing
# - POST database failures (curated read, library upsert, upsert anomaly,
#   profile mark) return 502; a failed library write does not mark the profile
#
# What is covered:
# - Happy path, expected empty state, pagination, validation, parent not
#   found, upstream failure and timeout, the resolution rules and the
#   database clients. The real queries and functions are in
#   test/integration/test_onboarding_integration.py
#
# Database access is overridden through get_db and get_user_db (core/auth.py,
# core/database.py), with separate mocks to tell which client runs each query.
#
# Run with: pytest test/routes/test_onboarding.py -v
#
# SEE: routes/onboarding.py, services/onboarding_service.py,
# models/onboarding.py

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import requests
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import app
from core import provider_session
from core.auth import get_current_profile, get_current_user_id, get_user_db
from core.cache import get_redis
from core.database import get_db
from core.provider_session import ProviderSession
from core.search_provider import PROVIDER_ERRORS, get_search_provider
from models.onboarding import MAX_ARTIST_IDS
from models.profiles import Profile
from services import onboarding_service
from test.fake_provider_http import FakeAdapter, FakeClock, body_response

client = TestClient(app, raise_server_exceptions=False)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_NO_STORE = "no-store"
_PRIVATE = "private, no-cache"
_MARKED_AT = "2026-10-30T12:00:00+00:00"
_EARLIER = "2026-01-01T00:00:00+00:00"

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


def _thumb(name: str) -> str:
    return f"https://lh3.googleusercontent.com/{name}=w60-h60-l90-rj"


def _curated_rows(count=5):
    return [
        {
            "id": f"UCcur{i}",
            "name": f"Curated {i}",
            "thumbnail_url": _thumb(f"cur{i}"),
            "position": i,
        }
        for i in range(1, count + 1)
    ]


_CURATED_IDS = [row["id"] for row in _curated_rows()]


def _profile(completed_at=None):
    return Profile(
        id=_USER_ID,
        role="user",
        created_at=_EARLIER,
        updated_at=_EARLIER,
        onboarding_completed_at=completed_at,
    )


# --- Fakes -------------------------------------------------------------------


class _Query:
    def __init__(self, db: FakeDb, table: str):
        self.db = db
        self.table = table
        self.verb = "select"
        self.args: dict = {}
        self.filters: list[tuple] = []
        self.max_rows: int | None = None
        self.count = None

    def select(self, columns, count=None):
        self.args["columns"] = columns
        self.count = count
        return self

    def upsert(self, rows, on_conflict=None):
        self.verb = "upsert"
        self.args.update(rows=rows, on_conflict=on_conflict)
        return self

    def update(self, values):
        self.verb = "update"
        self.args["values"] = values
        return self

    def in_(self, column, values):
        self.filters.append(("in", column, list(values)))
        return self

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def is_(self, column, value):
        self.filters.append(("is", column, value))
        return self

    def or_(self, expression):
        self.filters.append(("or", expression))
        return self

    def order(self, column, desc=False):
        self.args.setdefault("orders", []).append((column, desc))
        return self

    def limit(self, count):
        self.max_rows = count
        return self

    def execute(self):
        self.db.queries.append(self)
        error = self.db.errors.get((self.table, self.verb))
        if error is not None:
            raise error
        return self.db.run(self)


class FakeDb:
    # Imitates the postgrest chain the service uses. curated is the table
    # onboarding_artists; profile_rows what a profiles select returns and
    # update_rows what a profiles update returns. errors maps (table, verb)
    # to the exception its execute() raises.
    def __init__(
        self, curated=(), profile_rows=None, update_rows=None, upsert_rows=None
    ):
        self.curated = list(curated)
        self.profile_rows = profile_rows
        self.update_rows = update_rows
        self.upsert_rows = upsert_rows
        self.errors: dict[tuple[str, str], Exception] = {}
        self.queries: list[_Query] = []

    def table(self, name):
        return _Query(self, name)

    def of(self, table, verb):
        return [q for q in self.queries if q.table == table and q.verb == verb]

    def run(self, query: _Query):
        if query.table == "onboarding_artists":
            return self._curated(query)
        if query.table == "library_items":
            assert query.verb == "upsert"
            rows = query.args["rows"]
            return SimpleNamespace(
                data=rows if self.upsert_rows is None else self.upsert_rows
            )
        assert query.table == "profiles"
        if query.verb == "update":
            if self.update_rows is not None:
                return SimpleNamespace(data=self.update_rows)
            return SimpleNamespace(
                data=[{"onboarding_completed_at": _MARKED_AT}],
            )
        return SimpleNamespace(data=self.profile_rows or [])

    def _curated(self, query: _Query):
        rows = sorted(self.curated, key=lambda r: r["position"])
        for kind, *rest in query.filters:
            if kind == "in":
                rows = [r for r in rows if r[rest[0]] in rest[1]]
            elif kind == "or":
                # keyset_filter: "position.gt.N,and(...)".
                after = int(rest[0].split(",")[0].rsplit(".", 1)[1])
                rows = [r for r in rows if r["position"] > after]
        total = len(self.curated) if query.count else None
        if query.max_rows is not None:
            rows = rows[: query.max_rows]
        return SimpleNamespace(data=rows, count=total)


def _provider(pages=None, missing=()):
    # get_artist raises what the library raises for an id that is not there
    # (KeyError on 'contents'); _send_request, the probe, answers the
    # measured missing shape for those ids and fails for any other.
    provider = MagicMock()

    def get_artist(artist_id):
        if artist_id in missing:
            raise KeyError("contents")
        return (pages or {}).get(artist_id) or _artist_page(artist_id)

    def send_request(endpoint, body):
        assert endpoint == "browse"
        assert body["browseId"] in missing, body
        return _MISSING_BROWSE

    provider.get_artist.side_effect = get_artist
    provider._send_request.side_effect = send_request
    return provider


def _artist_page(artist_id):
    return {
        "name": f"Name of {artist_id}",
        "thumbnails": [{"url": _thumb(artist_id)}],
        "related": {"results": []},
        "albums": {"results": []},
    }


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
        get_user_db,
        get_current_user_id,
        get_current_profile,
        get_search_provider,
        get_redis,
    ):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture(autouse=True)
def _default_cache():
    app.dependency_overrides[get_redis] = lambda: _fake_cache()


def _use_list(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_id] = lambda: _USER_ID


def _use(db, user_db=None, provider=None, profile=None):
    # db: service-role (curated table, profile mark); user_db: the caller's
    # client (library).
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_db] = lambda: user_db or FakeDb()
    app.dependency_overrides[get_current_user_id] = lambda: _USER_ID
    app.dependency_overrides[get_current_profile] = lambda: profile or _profile()
    app.dependency_overrides[get_search_provider] = lambda: provider or _provider()
    return db, user_db


def _complete(ids, **kwargs):
    return client.post("/onboarding/complete", json={"artist_ids": ids}, **kwargs)


def _outside(count, start=1):
    return [f"UCout{i}" for i in range(start, start + count)]


def _library_rows(user_db):
    (query,) = user_db.of("library_items", "upsert")
    return query.args["rows"]


def _nothing_written(db, user_db):
    assert db.of("profiles", "update") == []
    assert user_db.of("library_items", "upsert") == []


# --- GET /onboarding/artists -------------------------------------------------


def test_get_onboarding_artists_returns_the_curated_list_ordered_by_position():
    rows = _curated_rows()
    db = FakeDb(curated=list(reversed(rows)))
    _use_list(db)

    response = client.get("/onboarding/artists")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == _PRIVATE
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert [i["id"] for i in data["items"]] == _CURATED_IDS
    assert data["items"][0] == {
        "id": "UCcur1",
        "name": "Curated 1",
        "thumbnail_url": _thumb("cur1"),
        "image": {
            "small": "https://lh3.googleusercontent.com/cur1=w192-h192-p-l90-rj",
            "medium": "https://lh3.googleusercontent.com/cur1=w600-h600-p-l90-rj",
            "large": "https://lh3.googleusercontent.com/cur1=w1320-h1320-p-l90-rj",
            "color": None,
        },
    }
    assert data["page"] == {
        "limit": 50,
        "next_cursor": None,
        "has_more": False,
        "total": 5,
    }


def test_get_onboarding_artists_without_rows_returns_an_empty_first_page():
    _use_list(FakeDb())

    response = client.get("/onboarding/artists")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [],
            "page": {"limit": 50, "next_cursor": None, "has_more": False, "total": 0},
        },
    }


def test_get_onboarding_artists_reads_with_the_service_role_client():
    db = FakeDb(curated=_curated_rows())
    _use_list(db)

    client.get("/onboarding/artists")

    (query,) = db.of("onboarding_artists", "select")
    assert query.args["columns"] == "id, name, thumbnail_url, position"
    assert query.args["orders"] == [("position", False), ("id", False)]
    assert query.max_rows == 51


def test_get_onboarding_artists_first_page_has_more_and_total():
    _use_list(FakeDb(curated=_curated_rows(5)))

    response = client.get("/onboarding/artists?limit=2")

    page = response.json()["data"]["page"]
    assert [i["id"] for i in response.json()["data"]["items"]] == ["UCcur1", "UCcur2"]
    assert page["has_more"] is True
    assert page["total"] == 5
    assert page["next_cursor"]


def test_get_onboarding_artists_second_page_with_the_cursor_has_null_total():
    _use_list(FakeDb(curated=_curated_rows(5)))
    cursor = client.get("/onboarding/artists?limit=2").json()["data"]["page"][
        "next_cursor"
    ]

    response = client.get(f"/onboarding/artists?limit=2&cursor={cursor}")

    data = response.json()["data"]
    assert [i["id"] for i in data["items"]] == ["UCcur3", "UCcur4"]
    assert data["page"]["total"] is None
    assert data["page"]["has_more"] is True


def test_get_onboarding_artists_last_page_has_no_cursor():
    _use_list(FakeDb(curated=_curated_rows(5)))
    cursor = client.get("/onboarding/artists?limit=4").json()["data"]["page"][
        "next_cursor"
    ]

    response = client.get(f"/onboarding/artists?limit=4&cursor={cursor}")

    data = response.json()["data"]
    assert [i["id"] for i in data["items"]] == ["UCcur5"]
    assert data["page"]["has_more"] is False
    assert data["page"]["next_cursor"] is None


def test_get_onboarding_artists_invalid_cursor_is_422_without_touching_the_database():
    db = FakeDb(curated=_curated_rows())
    _use_list(db)

    response = client.get("/onboarding/artists?cursor=garbage")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_cursor"}
    assert db.queries == []


def test_get_onboarding_artists_invalid_limit_is_invalid_request():
    _use_list(FakeDb())

    response = client.get("/onboarding/artists?limit=0")

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}


def test_get_onboarding_artists_database_failure_returns_upstream_error():
    db = FakeDb(curated=_curated_rows())
    db.errors[("onboarding_artists", "select")] = APIError({"message": "boom"})
    _use_list(db)

    response = client.get("/onboarding/artists")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert response.headers["Cache-Control"] == _NO_STORE


def test_get_onboarding_artists_database_timeout_returns_upstream_timeout():
    db = FakeDb(curated=_curated_rows())
    db.errors[("onboarding_artists", "select")] = httpx.ReadTimeout("slow")
    _use_list(db)

    response = client.get("/onboarding/artists")

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}


def test_get_onboarding_artists_without_token_returns_unauthorized():
    app.dependency_overrides[get_db] = lambda: FakeDb(curated=_curated_rows())

    response = client.get("/onboarding/artists")

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    assert response.headers["Cache-Control"] == _NO_STORE


def test_get_onboarding_artists_sends_private_no_cache():
    _use_list(FakeDb())

    assert client.get("/onboarding/artists").headers["Cache-Control"] == _PRIVATE


def test_get_onboarding_artists_carries_the_stored_color(image_color_db, color_checks):
    _use_list(FakeDb(curated=_curated_rows()))

    color_checks.carries(lambda: client.get("/onboarding/artists"))


def test_get_onboarding_artists_reads_no_colors_when_there_is_nothing_to_read(
    image_color_db, color_checks
):
    _use_list(FakeDb())

    color_checks.reads_nothing(lambda: client.get("/onboarding/artists"))


def test_get_onboarding_artists_survives_a_failed_color_read(
    image_color_db, color_checks
):
    _use_list(FakeDb(curated=_curated_rows()))

    color_checks.survives_read_failure(lambda: client.get("/onboarding/artists"))


# --- POST /onboarding/complete: happy paths ----------------------------------


def test_complete_with_five_curated_ids_saves_five_artists_and_marks_the_profile():
    db = FakeDb(curated=_curated_rows())
    user_db = FakeDb()
    provider = _provider()
    _use(db, user_db, provider)

    response = _complete(_CURATED_IDS)

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == _PRIVATE
    assert response.json() == {
        "ok": True,
        "data": {"onboarding_completed_at": _MARKED_AT, "saved": 5, "skipped": []},
    }
    provider.get_artist.assert_not_called()
    (read,) = db.of("onboarding_artists", "select")
    assert ("in", "id", _CURATED_IDS) in read.filters
    (upsert,) = user_db.of("library_items", "upsert")
    assert upsert.args["on_conflict"] == "user_id,kind,external_id"
    assert upsert.args["rows"] == [
        {
            "user_id": _USER_ID,
            "kind": "artist",
            "external_id": row["id"],
            "title": row["name"],
            "source": "external",
            "thumbnail_url": row["thumbnail_url"],
        }
        for row in _curated_rows()
    ]
    (mark,) = db.of("profiles", "update")
    assert list(mark.args["values"]) == ["onboarding_completed_at"]
    assert ("eq", "id", _USER_ID) in mark.filters
    assert ("is", "onboarding_completed_at", "null") in mark.filters


def test_complete_saves_the_rows_without_a_photo_in_their_own_upsert_without_the_column():
    curated = _curated_rows()
    curated[0]["thumbnail_url"] = None
    curated[3]["thumbnail_url"] = None
    db = FakeDb(curated=curated)
    user_db = FakeDb()
    _use(db, user_db, _provider())

    response = _complete(_CURATED_IDS)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["saved"] == 5
    with_photo, without_photo = user_db.of("library_items", "upsert")
    assert [r["external_id"] for r in with_photo.args["rows"]] == [
        "UCcur2",
        "UCcur3",
        "UCcur5",
    ]
    assert all("thumbnail_url" in r for r in with_photo.args["rows"])
    assert [r["external_id"] for r in without_photo.args["rows"]] == [
        "UCcur1",
        "UCcur4",
    ]
    assert all("thumbnail_url" not in r for r in without_photo.args["rows"])
    for query in (with_photo, without_photo):
        assert query.args["on_conflict"] == "user_id,kind,external_id"


def test_complete_with_no_photo_at_all_makes_a_single_upsert_without_the_column():
    curated = _curated_rows()
    for row in curated:
        row["thumbnail_url"] = None
    user_db = FakeDb()
    _use(FakeDb(curated=curated), user_db, _provider())

    response = _complete(_CURATED_IDS)

    assert response.status_code == 200, response.text
    (upsert,) = user_db.of("library_items", "upsert")
    assert len(upsert.args["rows"]) == 5
    assert all("thumbnail_url" not in r for r in upsert.args["rows"])


def test_complete_with_ten_ids_calls_the_provider_only_for_the_ones_outside_the_list():
    db = FakeDb(curated=_curated_rows())
    user_db = FakeDb()
    provider = _provider()
    outside = _outside(5)
    _use(db, user_db, provider)

    response = _complete([*outside[:2], *_CURATED_IDS, *outside[2:]])

    assert response.status_code == 200, response.text
    assert response.json()["data"]["saved"] == 10
    assert sorted(c.args[0] for c in provider.get_artist.call_args_list) == outside
    rows = _library_rows(user_db)
    # The body order is kept; the title and the photo of an outside artist
    # come from its page.
    assert [r["external_id"] for r in rows] == [
        *outside[:2],
        *_CURATED_IDS,
        *outside[2:],
    ]
    assert rows[0]["title"] == "Name of UCout1"
    assert rows[0]["thumbnail_url"].startswith("https://lh3.googleusercontent.com/")
    assert rows[2]["title"] == "Curated 1"


def test_complete_with_twenty_ids_outside_the_list_saves_twenty():
    db = FakeDb()
    user_db = FakeDb()
    provider = _provider()
    _use(db, user_db, provider)

    response = _complete(_outside(MAX_ARTIST_IDS))

    assert response.status_code == 200, response.text
    assert response.json()["data"]["saved"] == 20
    assert provider.get_artist.call_count == 20
    assert len(_library_rows(user_db)) == 20


def test_complete_uses_the_user_id_of_the_token():
    db = FakeDb(curated=_curated_rows())
    user_db = FakeDb()
    _use(db, user_db)

    _complete(_CURATED_IDS)

    assert {r["user_id"] for r in _library_rows(user_db)} == {_USER_ID}
    (mark,) = db.of("profiles", "update")
    assert ("eq", "id", _USER_ID) in mark.filters


def test_complete_writes_the_library_as_the_user_and_the_mark_as_service_role():
    db = FakeDb(curated=_curated_rows())
    user_db = FakeDb()
    _use(db, user_db)

    _complete(_CURATED_IDS)

    assert {q.table for q in db.queries} == {"onboarding_artists", "profiles"}
    assert {q.table for q in user_db.queries} == {"library_items"}


def test_complete_when_already_completed_keeps_the_timestamp_and_does_not_duplicate():
    db = FakeDb(curated=_curated_rows())
    user_db = FakeDb()
    _use(db, user_db, profile=_profile(completed_at=_EARLIER))

    response = _complete(_CURATED_IDS)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["onboarding_completed_at"] == _EARLIER
    assert db.of("profiles", "update") == []
    (upsert,) = user_db.of("library_items", "upsert")
    assert upsert.args["on_conflict"] == "user_id,kind,external_id"


def test_complete_when_another_request_marked_it_rereads_the_timestamp():
    db = FakeDb(
        curated=_curated_rows(),
        update_rows=[],
        profile_rows=[{"onboarding_completed_at": _EARLIER}],
    )
    _use(db)

    response = _complete(_CURATED_IDS)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["onboarding_completed_at"] == _EARLIER
    (reread,) = db.of("profiles", "select")
    assert ("eq", "id", _USER_ID) in reread.filters


def test_complete_skips_a_missing_artist_and_saves_the_rest(caplog):
    db = FakeDb(curated=_curated_rows())
    user_db = FakeDb()
    _use(db, user_db, _provider(missing={"UCgone"}))

    with caplog.at_level(logging.INFO):
        response = _complete([*_CURATED_IDS, "UCgone"])

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "onboarding_completed_at": _MARKED_AT,
        "saved": 5,
        "skipped": ["UCgone"],
    }
    assert "UCgone" not in {r["external_id"] for r in _library_rows(user_db)}
    assert any(
        "onboarding skipped artist UCgone" in r.getMessage() for r in caplog.records
    )


def test_complete_with_four_valid_and_one_missing_is_too_few_artists_and_writes_nothing():
    db = FakeDb(curated=_curated_rows())
    user_db = FakeDb()
    _use(db, user_db, _provider(missing={"UCgone"}))

    response = _complete([*_CURATED_IDS[:4], "UCgone"])

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "too_few_artists"}
    _nothing_written(db, user_db)


# --- POST /onboarding/complete: invalid input --------------------------------


def _assert_invalid_and_untouched(response, db, user_db, provider):
    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}
    assert db.queries == []
    assert user_db.queries == []
    provider.get_artist.assert_not_called()


def _untouched_setup():
    db, user_db, provider = FakeDb(curated=_curated_rows()), FakeDb(), _provider()
    _use(db, user_db, provider)
    return db, user_db, provider


def test_complete_with_repeated_ids_is_invalid_request_and_touches_nothing():
    db, user_db, provider = _untouched_setup()

    response = _complete([*_CURATED_IDS[:4], _CURATED_IDS[0]])

    _assert_invalid_and_untouched(response, db, user_db, provider)


def test_complete_with_four_ids_is_invalid_request_and_touches_nothing():
    db, user_db, provider = _untouched_setup()

    response = _complete(_CURATED_IDS[:4])

    _assert_invalid_and_untouched(response, db, user_db, provider)


def test_complete_with_more_than_the_maximum_is_invalid_request():
    db, user_db, provider = _untouched_setup()

    response = _complete(_outside(MAX_ARTIST_IDS + 1))

    _assert_invalid_and_untouched(response, db, user_db, provider)


def test_complete_with_a_malformed_id_is_invalid_request_and_touches_nothing():
    db, user_db, provider = _untouched_setup()

    response = _complete([*_CURATED_IDS[:4], "notanartist"])

    _assert_invalid_and_untouched(response, db, user_db, provider)


@pytest.mark.parametrize(
    "bad",
    [
        'UCa"b',
        "UCa,b",
        "UCa(b)",
        "UCa b",
        "UCa\nb",
        "UCa'b",
        "UCa.b",
        "UCa*b",
        "UCa\\b",
        "UCa:b",
    ],
)
def test_complete_with_an_id_that_would_break_the_in_filter_is_invalid_request(bad):
    db, user_db, provider = _untouched_setup()

    response = _complete([*_CURATED_IDS[:4], bad])

    _assert_invalid_and_untouched(response, db, user_db, provider)


def test_complete_accepts_the_characters_of_a_real_provider_id():
    db = FakeDb()
    user_db = FakeDb()
    _use(db, user_db)

    response = _complete(
        [
            "UCabcdefghijklmnopqrstuv",
            "UC_-0123456789ABCDEFGHIJ",
            "MPLAUCabcdefghijklmnopqr",
            "UCx",
            "UCy",
        ]
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("body", [{}, {"artist_ids": []}, {"artist_ids": None}])
def test_complete_without_artist_ids_is_invalid_request(body):
    db, user_db, provider = _untouched_setup()

    response = client.post("/onboarding/complete", json=body)

    _assert_invalid_and_untouched(response, db, user_db, provider)


def test_complete_with_a_user_id_in_the_body_is_invalid_request():
    db, user_db, provider = _untouched_setup()

    response = client.post(
        "/onboarding/complete",
        json={"artist_ids": _CURATED_IDS, "user_id": "someone-else"},
    )

    _assert_invalid_and_untouched(response, db, user_db, provider)


def test_complete_without_token_returns_unauthorized():
    app.dependency_overrides[get_db] = lambda: FakeDb()
    app.dependency_overrides[get_search_provider] = lambda: _provider()

    response = _complete(_CURATED_IDS)

    assert response.status_code == 401
    assert response.json() == {"ok": False, "reason": "unauthorized"}
    assert response.headers["Cache-Control"] == _NO_STORE


def test_complete_sends_private_no_cache():
    db = FakeDb(curated=_curated_rows())
    _use(db)

    assert _complete(_CURATED_IDS).headers["Cache-Control"] == _PRIVATE


# --- POST /onboarding/complete: parent not found ------------------------------


def test_complete_without_a_profile_returns_profile_not_found():
    db = FakeDb(curated=_curated_rows())
    user_db = FakeDb(profile_rows=[])
    provider = _provider()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_user_db] = lambda: user_db
    app.dependency_overrides[get_current_user_id] = lambda: _USER_ID
    app.dependency_overrides[get_search_provider] = lambda: provider

    response = _complete(_CURATED_IDS)

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "profile_not_found"}
    _nothing_written(db, user_db)
    provider.get_artist.assert_not_called()


# --- POST /onboarding/complete: upstream failures ------------------------------


def _failing_provider(error):
    provider = _provider()
    provider.get_artist.side_effect = error
    return provider


def test_complete_provider_server_error_returns_upstream_error_and_writes_nothing():
    db, user_db = FakeDb(curated=_curated_rows()), FakeDb()
    provider = _failing_provider(PROVIDER_ERRORS[0]("server error"))
    _use(db, user_db, provider)

    response = _complete([*_CURATED_IDS, *_outside(1)])

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert response.headers["Cache-Control"] == _NO_STORE
    provider._send_request.assert_not_called()
    _nothing_written(db, user_db)


def test_complete_provider_timeout_returns_upstream_timeout_and_writes_nothing():
    db, user_db = FakeDb(curated=_curated_rows()), FakeDb()
    provider = _failing_provider(requests.exceptions.ReadTimeout("slow"))
    _use(db, user_db, provider)

    response = _complete([*_CURATED_IDS, *_outside(1)])

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    provider._send_request.assert_not_called()
    _nothing_written(db, user_db)


def test_complete_keyerror_with_an_existing_page_returns_upstream_error():
    db, user_db = FakeDb(curated=_curated_rows()), FakeDb()
    provider = _failing_provider(KeyError("contents"))
    provider._send_request.side_effect = None
    provider._send_request.return_value = _EXISTING_BROWSE
    _use(db, user_db, provider)

    response = _complete([*_CURATED_IDS, *_outside(1)])

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    _nothing_written(db, user_db)


def test_complete_probe_failure_returns_upstream_error():
    db, user_db = FakeDb(curated=_curated_rows()), FakeDb()
    provider = _failing_provider(KeyError("contents"))
    provider._send_request.side_effect = PROVIDER_ERRORS[0]("server error")
    _use(db, user_db, provider)

    response = _complete([*_CURATED_IDS, *_outside(1)])

    assert response.status_code == 502
    _nothing_written(db, user_db)


def test_complete_probe_timeout_returns_upstream_timeout():
    db, user_db = FakeDb(curated=_curated_rows()), FakeDb()
    provider = _failing_provider(KeyError("contents"))
    provider._send_request.side_effect = requests.exceptions.ReadTimeout("slow")
    _use(db, user_db, provider)

    response = _complete([*_CURATED_IDS, *_outside(1)])

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    _nothing_written(db, user_db)


def test_complete_worker_calls_share_the_request_budget(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(provider_session, "_now", clock)
    # One worker keeps the order deterministic: the first call, made from a
    # worker, spends the budget and the third never leaves.
    monkeypatch.setattr(onboarding_service, "_PARALLEL_CALLS", 1)
    adapter = FakeAdapter(lambda request: (clock.advance(7), body_response(request))[1])
    session = ProviderSession(
        connect_timeout=3.05, read_timeout=10.0, slow_threshold=60
    )
    session.mount("https://", adapter)
    provider = MagicMock()

    def get_artist(artist_id):
        session.post("https://provider.test/v1/browse", json={})
        return _artist_page(artist_id)

    provider.get_artist.side_effect = get_artist
    db, user_db = FakeDb(), FakeDb()
    _use(db, user_db, provider)

    response = _complete(_outside(5))

    assert response.status_code == 504
    assert response.json() == {"ok": False, "reason": "upstream_timeout"}
    assert len(adapter.calls) == 2
    _nothing_written(db, user_db)


# --- POST /onboarding/complete: database failures ------------------------------


def test_complete_curated_read_failure_returns_upstream_error_without_calling_the_provider():
    db, user_db, provider = FakeDb(curated=_curated_rows()), FakeDb(), _provider()
    db.errors[("onboarding_artists", "select")] = APIError({"message": "boom"})
    _use(db, user_db, provider)

    response = _complete([*_CURATED_IDS, *_outside(1)])

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    provider.get_artist.assert_not_called()
    _nothing_written(db, user_db)


def test_complete_library_upsert_failure_returns_upstream_error_and_does_not_mark_the_profile():
    db, user_db = FakeDb(curated=_curated_rows()), FakeDb()
    user_db.errors[("library_items", "upsert")] = APIError({"message": "boom"})
    _use(db, user_db)

    response = _complete(_CURATED_IDS)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert db.of("profiles", "update") == []


def test_complete_library_upsert_timeout_returns_upstream_timeout():
    db, user_db = FakeDb(curated=_curated_rows()), FakeDb()
    user_db.errors[("library_items", "upsert")] = httpx.ReadTimeout("slow")
    _use(db, user_db)

    response = _complete(_CURATED_IDS)

    assert response.status_code == 504
    assert db.of("profiles", "update") == []


def test_complete_library_upsert_anomaly_returns_upstream_error():
    db, user_db = FakeDb(curated=_curated_rows()), FakeDb(upsert_rows=[])
    _use(db, user_db)

    response = _complete(_CURATED_IDS)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}
    assert db.of("profiles", "update") == []


def test_complete_profile_mark_failure_returns_upstream_error():
    db = FakeDb(curated=_curated_rows())
    db.errors[("profiles", "update")] = APIError({"message": "boom"})
    _use(db)

    response = _complete(_CURATED_IDS)

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_complete_mark_with_no_timestamp_after_the_race_returns_upstream_error():
    db = FakeDb(
        curated=_curated_rows(),
        update_rows=[],
        profile_rows=[{"onboarding_completed_at": None}],
    )
    _use(db)

    response = _complete(_CURATED_IDS)

    assert response.status_code == 502
