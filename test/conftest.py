# test/conftest.py
#
# Harness shared by every mock test: a fake of the image color table and the
# guard that keeps any test from downloading an image.
#
# Tested:
# - Every mock test gets a FakeImageColorDb through the get_image_color_db
#   override (core/image_color_dependency.py), so no test reaches a real
#   database for the colors
# - services.image_color_service._fetch_image is replaced by a stub that
#   records its calls; a test that triggers a download it did not allow
#   fails at teardown
# - The fake records one entry per upsert (upsert_calls) and per update
#   (update_calls, the write of a failure, which only touches a row still
#   without color), can fail the write of chosen keys only (write_error_for)
#   and sets an Event after each write, because the color service writes from
#   several pool threads
# - The claim of the fake takes back a key without color and without
#   failed_at older than p_abandoned_before, like migration 044, and records
#   its parameters (claim_params); null_for_all = "abandoned" simulates
#   abandoned keys; ColorChecks checks that an abandoned key shortens the
#   header and is claimed again
# - ColorChecks.shortens_cache_control checks that a response with a pending
#   color, or one being computed by another request, caps a plain max-age,
#   that a recent failure does not, and leaves every other header alone
# - The fake read returns failed_at like the RPC of migration 043, and
#   null_for_all simulates every key being in progress or recently failed
# - By default the fake claim returns no key, so no existing test downloads
#   anything; a test opts in with claims_like_the_database and
#   allow_image_downloads
#
# What is covered:
# - Fixtures and helpers only, no tests of its own
#
# Integration tests (marker integration) get neither the override nor the
# stub from here: test/integration/conftest.py has its own.
#
# SEE: services/image_color_service.py, core/image_color_dependency.py

import re
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app import app
from core.config import settings
from core.image_color_dependency import get_image_color_db
from services import image_color_service


class _Call:
    def __init__(self, run):
        self._run = run

    def execute(self):
        return SimpleNamespace(data=self._run())


class _FakeTable:
    def __init__(self, db: FakeImageColorDb):
        self._db = db

    def upsert(self, rows, on_conflict=None):
        def run():
            # Several pool threads write at once.
            with self._db.lock:
                self._db.upsert_calls.append([dict(row) for row in rows])
                for row in rows:
                    if row["image_key"] in self._db.write_error_for:
                        raise httpx.ConnectError("database down")
                self._db.writes.extend(rows)
                for row in rows:
                    self._db.stored[row["image_key"]] = dict(row)
            self._db.written.set()
            return rows

        return _Call(run)

    def update(self, row):
        return _FakeUpdate(self._db, row)


class _FakeUpdate:
    # .update(row).eq("image_key", key).is_("color", "null")
    # .eq("attempted_at", claim): the only shape the service uses, the write
    # of a failure. Like the database, it touches
    # only the row that matches the filters and does nothing when none does.
    def __init__(self, db: FakeImageColorDb, row: dict):
        self._db = db
        self._row = dict(row)
        self._filters: list[tuple[str, str, object]] = []

    def eq(self, column, value):
        self._filters.append(("eq", column, value))
        return self

    def is_(self, column, value):
        assert value == "null", "the fake only supports is_(column, 'null')"
        self._filters.append(("is", column, None))
        return self

    def execute(self):
        db = self._db
        with db.lock:
            db.update_calls.append(dict(self._row))
            if self._row["image_key"] in db.write_error_for:
                raise httpx.ConnectError("database down")
            db.writes.append(dict(self._row))
            matched = []
            for key, stored in db.stored.items():
                if all(
                    (stored[column] == value)
                    if kind == "eq"
                    else (stored[column] is None)
                    for kind, column, value in self._filters
                ):
                    matched.append(key)
            for key in matched:
                db.stored[key] = {**db.stored[key], **self._row}
        db.written.set()
        return SimpleNamespace(data=[db.stored[key] for key in matched])


class FakeImageColorDb:
    # Imitates the two calls the color service makes: .rpc(name, params) and
    # .table("image_colors").upsert(rows, on_conflict=...) (a color) and
    # .table("image_colors").update(row).eq(...).is_(...).eq(...) (a failure,
    # which never overwrites a color nor a claim taken again). stored maps an image_key to its row
    # ({image_key, color, attempted_at, failed_at}).
    def __init__(self):
        self.stored: dict[str, dict] = {}
        self.read_error: Exception | None = None
        self.claim_error: Exception | None = None
        self.write_error: Exception | None = None
        # Only the write (upsert or update) of these keys fails.
        self.write_error_for: set[str] = set()
        self.lock = threading.Lock()
        # Set after every write.
        self.written = threading.Event()
        # One list of rows per upsert call.
        self.upsert_calls: list[list[dict]] = []
        # One row per update call (the write of a failure).
        self.update_calls: list[dict] = []
        self.claims_like_the_database = False
        # When set, every key asked for comes back with this color.
        self.color_for_all: str | None = None
        # "in_progress", "failed" or "abandoned": every key asked for comes
        # back without color, claimed now (in progress, failed) or claimed
        # image_color_abandoned_after + 1 seconds ago (abandoned), with
        # failed_at set only for "failed". color_for_all wins.
        self.null_for_all: str | None = None
        self.reads: list[list[str]] = []
        self.claims: list[list[str]] = []
        # The parameters of every claim, as the service sent them.
        self.claim_params: list[dict] = []
        self.writes: list[dict] = []

    def store(self, key: str, color: str | None, *, attempted_at=None, failed=False):
        stamp = (attempted_at or datetime.now(UTC)).isoformat()
        self.stored[key] = {
            "image_key": key,
            "color": color,
            "attempted_at": stamp,
            "failed_at": stamp if failed else None,
        }

    def rpc(self, name: str, params: dict, **_):
        if name == "get_image_colors":
            return _Call(lambda: self._read(params))
        if name == "claim_image_colors":
            return _Call(lambda: self._claim(params))
        raise AssertionError(f"unexpected rpc {name}")

    def table(self, name: str):
        assert name == "image_colors"
        if self.write_error is not None:
            raise self.write_error
        return _FakeTable(self)

    def _read(self, params: dict):
        if self.read_error is not None:
            raise self.read_error
        self.reads.append(list(params["p_keys"]))
        if self.color_for_all is not None:
            return [
                {
                    "image_key": key,
                    "color": self.color_for_all,
                    "attempted_at": datetime.now(UTC).isoformat(),
                    "failed_at": None,
                }
                for key in params["p_keys"]
            ]
        if self.null_for_all is not None:
            moment = datetime.now(UTC)
            if self.null_for_all == "abandoned":
                moment -= timedelta(seconds=settings.image_color_abandoned_after + 1)
            now = moment.isoformat()
            return [
                {
                    "image_key": key,
                    "color": None,
                    "attempted_at": now,
                    "failed_at": now if self.null_for_all == "failed" else None,
                }
                for key in params["p_keys"]
            ]
        return [
            {
                "image_key": key,
                "color": self.stored[key]["color"],
                "attempted_at": self.stored[key]["attempted_at"],
                "failed_at": self.stored[key]["failed_at"],
            }
            for key in params["p_keys"]
            if key in self.stored
        ]

    def _claim(self, params: dict):
        if self.claim_error is not None:
            raise self.claim_error
        self.claims.append(list(params["p_keys"]))
        self.claim_params.append(dict(params))
        if not self.claims_like_the_database:
            return []
        claimed = []
        retry_before = datetime.fromisoformat(params["p_retry_before"])
        abandoned_before = datetime.fromisoformat(params["p_abandoned_before"])
        for key in dict.fromkeys(params["p_keys"]):
            row = self.stored.get(key)
            # Like migration 044: a key without color is taken back when its
            # last attempt is older than the retry window, or when it has no
            # failure and its claim is older than the abandoned threshold.
            stale = row is not None and (
                row["color"] is None
                and (
                    datetime.fromisoformat(row["attempted_at"]) < retry_before
                    or (
                        row["failed_at"] is None
                        and datetime.fromisoformat(row["attempted_at"])
                        < abandoned_before
                    )
                )
            )
            if row is None or stale:
                self.stored[key] = {
                    "image_key": key,
                    "color": None,
                    "attempted_at": params["p_attempted_at"],
                    "failed_at": None,
                }
                claimed.append({"image_key": key})
        return claimed


class DownloadStub:
    # Replaces _fetch_image. enabled is the function a test allows; without
    # one, a call is recorded and the teardown fails the test.
    def __init__(self):
        self.calls: list[str] = []
        self.unexpected: list[str] = []
        self.enabled = None

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.enabled is None:
            self.unexpected.append(url)
            raise image_color_service._ImageRejected("download not allowed")
        return self.enabled(url)


@pytest.fixture(autouse=True)
def _image_color_allowed_hosts(monkeypatch):
    # The setting has no default: the test URLs' host is allowed explicitly.
    monkeypatch.setattr(
        settings, "image_color_allowed_hosts", ["lh3.googleusercontent.com"]
    )


@pytest.fixture(autouse=True)
def _image_color_harness(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        yield None
        return
    db = FakeImageColorDb()
    stub = DownloadStub()
    app.dependency_overrides[get_image_color_db] = lambda: db
    monkeypatch.setattr(image_color_service, "_fetch_image", stub)
    request.node.image_color_db = db
    request.node.download_stub = stub
    yield db
    app.dependency_overrides.pop(get_image_color_db, None)
    assert stub.unexpected == [], "a test downloaded an image it did not allow"


@pytest.fixture
def image_color_db(request, _image_color_harness) -> FakeImageColorDb:
    return _image_color_harness


@pytest.fixture
def allow_image_downloads(request):
    stub: DownloadStub = request.node.download_stub

    def _allow(fn):
        stub.enabled = fn
        return stub

    return _allow


class ColorChecks:
    # The three checks every endpoint with images shares. call() makes the
    # request of the endpoint and returns the response.
    COLOR = "#ee0000"

    def __init__(self, db: FakeImageColorDb):
        self.db = db

    def carries(self, call) -> None:
        self.db.color_for_all = self.COLOR
        response = call()

        assert response.status_code == 200, response.text
        tiers = tier_dicts(response.json())
        assert tiers
        assert all(tier["color"] == self.COLOR for tier in tiers)
        assert len(self.db.reads) == 1
        assert self.db.claims == []

    def reads_nothing(self, call) -> None:
        self.db.color_for_all = self.COLOR
        response = call()

        assert response.status_code == 200, response.text
        assert all(tier["color"] is None for tier in tier_dicts(response.json()))
        assert self.db.reads == []
        assert self.db.claims == []

    @staticmethod
    def _shortened(header: str | None, ceiling: int) -> str | None:
        # Only the plain max-age is capped; private max-age (upnext), no-cache,
        # no-store and a missing header are left as they are.
        if header is not None and re.fullmatch(r"max-age=[0-9]+", header):
            return f"max-age={min(int(header.split('=')[1]), ceiling)}"
        return header

    def shortens_cache_control(self, call) -> None:
        # Sentinel 7: a value no real max-age has.
        previous = settings.image_color_pending_max_age
        settings.image_color_pending_max_age = 7
        try:
            self.db.color_for_all = self.COLOR
            full = call().headers.get("cache-control")
            # With every color stored the header is the route's own: a plain
            # max-age, when there is one, is longer than the sentinel.
            if full is not None and re.fullmatch(r"max-age=[0-9]+", full):
                assert int(full.split("=")[1]) > 7
            self.db.color_for_all = None
            pending = call()
            assert pending.status_code == 200, pending.text
            assert pending.headers.get("cache-control") == self._shortened(full, 7)
            # Another request is computing the colors: the keys are not
            # claimed from here, and the header is still short.
            claims_before = len(self.db.claims)
            self.db.null_for_all = "in_progress"
            computing = call()
            assert computing.status_code == 200, computing.text
            assert computing.headers.get("cache-control") == self._shortened(full, 7)
            assert len(self.db.claims) == claims_before
            # Claimed longer ago than the abandoned threshold: claimed again
            # from here, and the header is short.
            claims_before = len(self.db.claims)
            self.db.null_for_all = "abandoned"
            abandoned = call()
            assert abandoned.status_code == 200, abandoned.text
            assert abandoned.headers.get("cache-control") == self._shortened(full, 7)
            assert len(self.db.claims) > claims_before
            # A failure inside the window is a permanent null: the route's
            # own header.
            self.db.null_for_all = "failed"
            failed_recently = call()
            assert failed_recently.status_code == 200, failed_recently.text
            assert failed_recently.headers.get("cache-control") == full
            self.db.null_for_all = None
            self.db.read_error = httpx.ConnectError("database down")
            failed = call()
            assert failed.status_code == 200, failed.text
            assert failed.headers.get("cache-control") == self._shortened(full, 7)
        finally:
            settings.image_color_pending_max_age = previous

    def survives_read_failure(self, call) -> None:
        baseline = call()
        assert baseline.status_code == 200, baseline.text
        assert tier_dicts(baseline.json())
        claims_before = len(self.db.claims)

        self.db.read_error = httpx.ConnectError("database down")
        response = call()

        assert response.status_code == 200, response.text
        assert response.json() == baseline.json()
        assert all(tier["color"] is None for tier in tier_dicts(response.json()))
        assert len(self.db.claims) == claims_before


@pytest.fixture
def color_checks(image_color_db) -> ColorChecks:
    return ColorChecks(image_color_db)


def tier_dicts(body) -> list[dict]:
    # Every dict of the JSON that carries the image tiers.
    found = []

    def walk(node):
        if isinstance(node, dict):
            if {"small", "medium", "large", "color"} <= node.keys():
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return found
