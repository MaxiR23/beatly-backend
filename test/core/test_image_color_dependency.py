# test/core/test_image_color_dependency.py
#
# Tests for the FastAPI dependency that opens the image color scope.
#
# Tested:
# - image_colors is a coroutine function (a plain def would run in the
#   threadpool, and no color would reach the response)
# - A sync endpoint that depends on it serves the stored color in the
#   response
# - The scope does not leak into a later request that lacks the dependency
# - The response is sent before the download starts, and a failing download
#   does not change that response
# - A route that sets max-age=100 goes out with the configured short max-age
#   while a color is pending, being computed by another request or the read
#   failed, with max-age=100 when the color is stored, and a route without
#   header gets none
# - The computation is scheduled once per request and stores the result for
#   the next one
#
# What is covered:
# - core/image_color_dependency.py, on a small app of its own
#
# Run with: pytest test/core/test_image_color_dependency.py -v
#
# SEE: core/image_color_dependency.py, core/image_color_scope.py

import inspect
from io import BytesIO

import httpx
from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import BaseModel, computed_field

from core.cache_control import set_max_age
from core.config import settings
from core.image_color_dependency import (
    ImageColors,
    get_image_color_db,
    image_colors,
    with_image_colors,
)
from core.thumbnails import image_tiers
from models.images import ImageTiers
from models.responses import ApiSuccess, ok_response

_HOST = "https://lh3.googleusercontent.com/abc"


class _Cover(BaseModel):
    url: str

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.url)


def _red_png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (192, 192), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def _app(db, order=None):
    app = FastAPI()
    app.dependency_overrides[get_image_color_db] = lambda: db

    @app.get("/with", response_model=ApiSuccess[_Cover])
    def with_colors(colors: ImageColors = Depends(image_colors)):  # noqa: B008
        return with_image_colors(colors, ok_response(_Cover(url=f"{_HOST}=w60-h60")))

    @app.get("/aged", response_model=ApiSuccess[_Cover])
    def aged(response: Response, colors: ImageColors = Depends(image_colors)):  # noqa: B008
        set_max_age(response, 100)
        return with_image_colors(colors, ok_response(_Cover(url=f"{_HOST}=w60-h60")))

    @app.get("/headerless", response_model=ApiSuccess[_Cover])
    def headerless(colors: ImageColors = Depends(image_colors)):  # noqa: B008
        return with_image_colors(colors, ok_response(_Cover(url=f"{_HOST}=w60-h60")))

    @app.get("/without", response_model=ApiSuccess[_Cover])
    def without_colors():
        return ok_response(_Cover(url=f"{_HOST}=w60-h60"))

    if order is None:
        return app

    async def record_when_sent(scope, receive, send):
        async def wrapped(message):
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                order.append("body_sent")
            await send(message)

        await app(scope, receive, wrapped)

    return record_when_sent


def test_the_dependency_is_a_coroutine_function():
    assert inspect.iscoroutinefunction(image_colors)


def test_a_sync_endpoint_serves_the_stored_color(image_color_db):
    image_color_db.store(_HOST, "#123456")

    response = TestClient(_app(image_color_db)).get("/with")

    assert response.status_code == 200
    assert response.json()["data"]["image"]["color"] == "#123456"
    assert image_color_db.reads == [[_HOST]]


def test_the_scope_does_not_leak_into_a_request_without_the_dependency(
    image_color_db,
):
    image_color_db.store(_HOST, "#123456")
    client = TestClient(_app(image_color_db))

    with_color = client.get("/with")
    without = client.get("/without")

    assert with_color.json()["data"]["image"]["color"] == "#123456"
    assert without.json()["data"]["image"]["color"] is None
    assert image_color_db.reads == [[_HOST]]


def test_the_response_is_sent_before_the_download_starts(
    image_color_db, allow_image_downloads
):
    order = []

    def slow_failure(url):
        order.append("fetch")
        raise httpx.ReadTimeout("slow")

    allow_image_downloads(slow_failure)
    image_color_db.claims_like_the_database = True

    response = TestClient(_app(image_color_db, order)).get("/with")

    assert response.status_code == 200
    assert response.json()["data"]["image"]["color"] is None
    assert order == ["body_sent", "fetch"]


def test_the_computed_color_is_served_on_the_next_request(
    image_color_db, allow_image_downloads
):
    stub = allow_image_downloads(lambda url: _red_png())
    image_color_db.claims_like_the_database = True
    client = TestClient(_app(image_color_db))

    first = client.get("/with")
    second = client.get("/with")

    assert first.json()["data"]["image"]["color"] is None
    assert second.json()["data"]["image"]["color"] == "#ee0000"
    assert stub.calls == [f"{_HOST}=w192-h192-p"]


def test_a_pending_color_shortens_the_max_age(image_color_db, monkeypatch):
    monkeypatch.setattr(settings, "image_color_pending_max_age", 5)

    response = TestClient(_app(image_color_db)).get("/aged")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "max-age=5"


def test_a_stored_color_keeps_the_max_age(image_color_db, monkeypatch):
    monkeypatch.setattr(settings, "image_color_pending_max_age", 5)
    image_color_db.store(_HOST, "#123456")

    response = TestClient(_app(image_color_db)).get("/aged")

    assert response.headers["cache-control"] == "max-age=100"


def test_a_color_being_computed_shortens_the_max_age(image_color_db, monkeypatch):
    monkeypatch.setattr(settings, "image_color_pending_max_age", 5)
    image_color_db.store(_HOST, None)

    response = TestClient(_app(image_color_db)).get("/aged")

    assert response.json()["data"]["image"]["color"] is None
    assert response.headers["cache-control"] == "max-age=5"
    assert image_color_db.claims == []


def test_a_failed_read_shortens_the_max_age(image_color_db, monkeypatch):
    monkeypatch.setattr(settings, "image_color_pending_max_age", 5)
    image_color_db.read_error = httpx.ConnectError("database down")

    response = TestClient(_app(image_color_db)).get("/aged")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "max-age=5"


def test_a_permanent_null_does_not_shorten_the_max_age(image_color_db, monkeypatch):
    monkeypatch.setattr(settings, "image_color_pending_max_age", 5)
    image_color_db.store(_HOST, None, failed=True)

    response = TestClient(_app(image_color_db)).get("/aged")

    assert response.json()["data"]["image"]["color"] is None
    assert response.headers["cache-control"] == "max-age=100"


def test_a_route_without_a_header_does_not_get_one(image_color_db):
    response = TestClient(_app(image_color_db)).get("/headerless")

    assert "cache-control" not in response.headers
