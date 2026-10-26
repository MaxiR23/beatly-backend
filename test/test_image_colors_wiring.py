# test/test_image_colors_wiring.py
#
# Guards the wiring of the image color dependency across the whole app.
#
# Tested:
# - Every route whose response schema carries ImageTiers or a RecentEntity
#   depends on image_colors
# - No other route depends on it
# - There are exactly 24 such routes
#
# What is covered:
# - The APIRouter objects of routes/*.py against the serialization schema of each response_model
#
# Run with: pytest test/test_image_colors_wiring.py -v
#
# SEE: core/image_color_dependency.py, docs/adr/021-image-colors-are-read-per-request-and-computed-after-the-response.md

import importlib
import pkgutil

from fastapi import APIRouter
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

import routes
from core.image_color_dependency import image_colors


def _carries_images(route: APIRoute) -> bool:
    if route.response_model is None:
        return False
    schema = TypeAdapter(route.response_model).json_schema(mode="serialization")
    text = str(schema)
    return "ImageTiers" in text or "RecentEntity" in text


def _depends_on_image_colors(route: APIRoute) -> bool:
    def walk(dependant):
        for sub in dependant.dependencies:
            if sub.call is image_colors or walk(sub):
                return True
        return False

    return walk(route.dependant)


def _routes():
    # The routers are read from the modules of routes/, not from app.routes:
    # the app wraps each included router in a private class.
    found = []
    for module_info in pkgutil.iter_modules(routes.__path__):
        module = importlib.import_module(f"routes.{module_info.name}")
        for value in vars(module).values():
            if isinstance(value, APIRouter):
                found.extend(r for r in value.routes if isinstance(r, APIRoute))
    return found


def test_every_route_with_image_tiers_depends_on_image_colors():
    missing = [
        f"{sorted(r.methods)} {r.path}"
        for r in _routes()
        if _carries_images(r) and not _depends_on_image_colors(r)
    ]

    assert missing == []


def test_no_route_without_image_tiers_depends_on_image_colors():
    extra = [
        f"{sorted(r.methods)} {r.path}"
        for r in _routes()
        if not _carries_images(r) and _depends_on_image_colors(r)
    ]

    assert extra == []


def test_there_are_24_routes_with_image_tiers():
    assert sum(1 for r in _routes() if _carries_images(r)) == 24
