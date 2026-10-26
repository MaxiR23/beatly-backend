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

from datetime import UTC, datetime
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
            self._db.writes.extend(rows)
            for row in rows:
                self._db.stored[row["image_key"]] = dict(row)
            return rows

        return _Call(run)


class FakeImageColorDb:
    # Imitates the two calls the color service makes: .rpc(name, params) and
    # .table("image_colors").upsert(rows, on_conflict=...). stored maps an
    # image_key to its row ({image_key, color, attempted_at, failed_at}).
    def __init__(self):
        self.stored: dict[str, dict] = {}
        self.read_error: Exception | None = None
        self.claim_error: Exception | None = None
        self.write_error: Exception | None = None
        self.claims_like_the_database = False
        # When set, every key asked for comes back with this color.
        self.color_for_all: str | None = None
        self.reads: list[list[str]] = []
        self.claims: list[list[str]] = []
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
                }
                for key in params["p_keys"]
            ]
        return [
            {
                "image_key": key,
                "color": self.stored[key]["color"],
                "attempted_at": self.stored[key]["attempted_at"],
            }
            for key in params["p_keys"]
            if key in self.stored
        ]

    def _claim(self, params: dict):
        if self.claim_error is not None:
            raise self.claim_error
        self.claims.append(list(params["p_keys"]))
        if not self.claims_like_the_database:
            return []
        claimed = []
        retry_before = datetime.fromisoformat(params["p_retry_before"])
        for key in dict.fromkeys(params["p_keys"]):
            row = self.stored.get(key)
            stale = row is not None and (
                row["color"] is None
                and datetime.fromisoformat(row["attempted_at"]) < retry_before
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
