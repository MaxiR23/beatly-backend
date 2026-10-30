# test/test_contract.py
#
# Tests for the global response contract and exception handlers.
#
# Tested:
# - A success response has ok:true and no "reason" key
# - An unknown route returns {"ok": false, "reason": "not_found"} with 404
# - A domain exception raised with a custom reason keeps that reason
#   and the status code defined by its class
# - A domain exception raised without a reason falls back to the
#   class default reason
# - A domain exception's status code comes from its own class, not
#   hardcoded to 404 (UpstreamError -> 502)
# - An unhandled exception returns {"ok": false, "reason":
#   "internal_error"} with 500, and never leaks the exception message
# - A request validation failure (bad query param type) returns
#   {"ok": false, "reason": "invalid_request"} with 422
# - OpenAPI lists image on exactly the 24 response models that carry a
#   provider image and mosaic on exactly the 6 that carry a mosaic, and on
#   no request model, RecentMetadata or TrackMetadata
# - Cache-Control: /health, an unknown route, a domain exception, a
#   validation failure and an unhandled exception all send no-store; all
#   34 user-data routes depend on private_no_cache
#
# What is covered:
# - models/responses.py envelope shape, core/exceptions.py mapping,
#   and the four global handlers wired in app.py
#
# Run with: pytest test/test_contract.py -v
#
# SEE: app.py, core/exceptions.py, models/responses.py

from fastapi.testclient import TestClient

from app import app
from core.cache_control import private_no_cache
from core.exceptions import NotFound, UpstreamError
from routes.activity import plays_router, recents_router
from routes.bug_reports import router as bug_reports_router
from routes.library import router as library_router
from routes.likes import router as likes_router
from routes.playlists import router as playlists_router
from routes.profile import router as profile_router
from routes.recommendations import router as recommendations_router

client = TestClient(app, raise_server_exceptions=False)

_CUSTOM_REASON = "custom_reason_here"
_SECRET_MESSAGE = "leaked db password abc123"


@app.get("/_test/raises-custom-reason")
def _raise_custom_reason():
    raise NotFound(_CUSTOM_REASON)


@app.get("/_test/raises-default-reason")
def _raise_default_reason():
    raise NotFound()


@app.get("/_test/raises-unhandled")
def _raise_unhandled():
    raise ValueError(_SECRET_MESSAGE)


@app.get("/_test/raises-upstream-error")
def _raise_upstream_error():
    raise UpstreamError()


@app.get("/_test/requires-int-query")
def _requires_int_query(count: int):
    return {"count": count}


def test_success_response_omits_reason():
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "reason" not in body


def test_unknown_route_returns_contract_shape():
    response = client.get("/this-route-does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "not_found"}


def test_domain_exception_with_custom_reason_keeps_reason_and_status():
    response = client.get("/_test/raises-custom-reason")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": _CUSTOM_REASON}


def test_domain_exception_without_reason_uses_class_default():
    response = client.get("/_test/raises-default-reason")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "reason": "not_found"}


def test_unhandled_exception_does_not_leak_details():
    response = client.get("/_test/raises-unhandled")

    assert response.status_code == 500
    assert response.json() == {"ok": False, "reason": "internal_error"}
    assert _SECRET_MESSAGE not in response.text


def test_domain_exception_status_code_comes_from_its_own_class():
    response = client.get("/_test/raises-upstream-error")

    assert response.status_code == 502
    assert response.json() == {"ok": False, "reason": "upstream_error"}


def test_invalid_query_param_returns_contract_shape():
    response = client.get("/_test/requires-int-query", params={"count": "not-a-number"})

    assert response.status_code == 422
    assert response.json() == {"ok": False, "reason": "invalid_request"}


# --- Cache-Control -----------------------------------------------------------

_USER_DATA_ROUTERS = [
    library_router,
    likes_router,
    playlists_router,
    profile_router,
    bug_reports_router,
    plays_router,
    recents_router,
    recommendations_router,
]


def _cache_control(response):
    return response.headers.get_list("cache-control")


def test_health_sends_no_store():
    response = client.get("/health")

    assert response.status_code == 200
    assert _cache_control(response) == ["no-store"]


def test_unknown_route_sends_no_store():
    response = client.get("/this-route-does-not-exist")

    assert response.status_code == 404
    assert _cache_control(response) == ["no-store"]


def test_domain_exception_sends_no_store():
    response = client.get("/_test/raises-custom-reason")

    assert response.status_code == 404
    assert _cache_control(response) == ["no-store"]


def test_invalid_request_sends_no_store():
    response = client.get("/_test/requires-int-query", params={"count": "x"})

    assert response.status_code == 422
    assert _cache_control(response) == ["no-store"]


def test_unhandled_exception_sends_no_store():
    # The Exception handler runs outside every user middleware, so this is
    # the test that proves the header is set by the handler itself.
    response = client.get("/_test/raises-unhandled")

    assert response.status_code == 500
    assert _cache_control(response) == ["no-store"]


def test_every_user_data_route_depends_on_private_no_cache():
    # Structural on purpose: the rule is per domain, so this proves all of
    # the domain's routes carry it without one behavior test per route.
    # Read from the routers, not from app.routes: FastAPI wraps an included
    # router in a lazy object there, while the router's own routes already
    # carry its dependencies=. Every domain is exercised through the app in
    # its own test file, so inclusion is covered there.
    routes = [route for router in _USER_DATA_ROUTERS for route in router.routes]

    assert len(routes) == 34
    for route in routes:
        calls = [dep.call for dep in route.dependant.dependencies]
        assert private_no_cache in calls, route.path


_IMAGE_SCHEMAS = {
    "Album",
    "AlbumRef",
    "Artist",
    "ArtistRef",
    "ArtistRelease",
    "ArtistSong",
    "GenrePlaylistListItem",
    "GenrePlaylistTrack",
    "LibraryEntry",
    "LibraryItem",
    "Like",
    "PlaylistPageTrack",
    "PublicAlbum",
    "PublicArtist",
    "PublicGenrePlaylist",
    "PublicPlaylistTrack",
    "PublicTrack",
    "RecommendationSeed",
    "RecommendedAlbum",
    "RecommendedArtist",
    "SearchAlbum",
    "SearchArtist",
    "SearchSong",
    "TrackRef",
}

_MOSAIC_SCHEMAS = {
    "GenrePlaylistListItem",
    "LibraryEntry",
    "PlaylistDetailWithCovers",
    "PlaylistListItem",
    "PublicGenrePlaylist",
    "PublicPlaylist",
}


def test_openapi_lists_image_and_mosaic_only_on_response_models():
    schemas = app.openapi()["components"]["schemas"]

    with_image = {n for n, s in schemas.items() if "image" in s.get("properties", {})}
    with_mosaic = {n for n, s in schemas.items() if "mosaic" in s.get("properties", {})}

    assert with_image == _IMAGE_SCHEMAS
    assert with_mosaic == _MOSAIC_SCHEMAS
    for name in ("RecentMetadata", "TrackMetadata"):
        assert not {"image", "mosaic"} & set(schemas[name].get("properties", {}))
    for name, schema in schemas.items():
        if name.endswith("Request"):
            assert not {"image", "mosaic"} & set(schema.get("properties", {}))
