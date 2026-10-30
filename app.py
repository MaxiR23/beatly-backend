import logging

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.cache_control import NO_STORE
from core.config import settings
from core.exceptions import HTTP_REASONS, AppError
from core.logging import setup_logging
from core.provider_session import clear_provider_deadline, provider_deadline
from models.responses import ApiSuccess, error_response, ok_response
from routes.activity import plays_router, recents_router
from routes.album import router as album_router
from routes.artist import router as artist_router
from routes.bug_reports import router as bug_reports_router
from routes.errors import router as errors_router
from routes.genres import genre_playlists_router
from routes.genres import router as genres_router
from routes.library import router as library_router
from routes.likes import router as likes_router
from routes.onboarding import router as onboarding_router
from routes.playlists import router as playlists_router
from routes.profile import router as profile_router
from routes.public import router as public_router
from routes.recommendations import router as recommendations_router
from routes.search import router as search_router
from routes.tracks import router as tracks_router

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


def warn_if_no_image_color_hosts() -> None:
    if not settings.image_color_allowed_hosts:
        logger.warning(
            "image_color_allowed_hosts is empty: no image colors will be computed"
        )


warn_if_no_image_color_hosts()


class ProviderTimeBudget:
    # Starts the budget every external provider call of this request shares
    # and ends it (ADR 024) at the first of three moments: the last body of
    # the response (or pathsend) going out, the http.disconnect coming
    # through receive, or the app returning or raising (the finally). Pure
    # ASGI and the innermost middleware on purpose: the deadline it sets
    # reaches the threadpool (which copies the context), and each clear
    # mutates the holder the ContextVar stores, which every copy of the
    # request's context shares. So the background tasks see it cleared even
    # when Starlette sent the body from a child task (a StreamingResponse
    # with spec_version < 2.4, which is what uvicorn sends). The clear in
    # receive is the one that covers the background of a stream cut by the
    # client under 2.3: the cut skips the last body, and the background and
    # the request-scope teardowns run inside the app, before any finally.
    # The finally covers what is left: tasks that copied the context and
    # outlive a request that ended without a last body. A BaseHTTPMiddleware
    # runs the app in a task of its own and could not do this. Read per
    # request so tests that patch settings see it.
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_ending_the_budget(message: Message) -> None:
            if message["type"] == "http.response.pathsend" or (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                clear_provider_deadline()
            await send(message)

        async def receive_ending_the_budget() -> Message:
            message = await receive()
            if message["type"] == "http.disconnect":
                clear_provider_deadline()
            return message

        with provider_deadline(settings.provider_request_budget):
            try:
                await self.app(scope, receive_ending_the_budget, send_ending_the_budget)
            finally:
                clear_provider_deadline()


app = FastAPI(title=settings.app_name)

# Registered first on purpose. Each registration goes to position 0, so
# this one stays the innermost, below every middleware added after it,
# in the same task as the route. test/test_provider_deadline_wiring.py
# pins it. See ADR 024.
app.add_middleware(ProviderTimeBudget)

# Registered before default_cache_control on purpose. Each registration goes
# to position 0, so the gzip ends up inside it and receives the whole body
# in one message, which is what lets minimum_size apply. Outside, it would
# see the stream a BaseHTTPMiddleware forwards, ignore minimum_size and
# compress even /health; nor can a BaseHTTPMiddleware go inside the gzip.
# test/test_compression.py pins this order. See ADR 020.
app.add_middleware(
    GZipMiddleware,
    minimum_size=settings.gzip_minimum_size,
    compresslevel=settings.gzip_compresslevel,
)


@app.middleware("http")
async def default_cache_control(request: Request, call_next) -> Response:
    # Acts only when nobody set the header (the uncached endpoints and the
    # errors); the cached and user-data routers set theirs on the success
    # path. No path lists.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", NO_STORE)
    return response


app.include_router(genres_router)
app.include_router(genre_playlists_router)
app.include_router(library_router)
app.include_router(likes_router)
app.include_router(playlists_router)
app.include_router(profile_router)
app.include_router(bug_reports_router)
app.include_router(plays_router)
app.include_router(recents_router)
app.include_router(search_router)
app.include_router(album_router)
app.include_router(artist_router)
app.include_router(tracks_router)
app.include_router(public_router)
app.include_router(errors_router)
app.include_router(recommendations_router)
app.include_router(onboarding_router)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.exception("app_error path=%s reason=%s", request.url.path, exc.reason)
    else:
        logger.warning("app_error path=%s reason=%s", request.url.path, exc.reason)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(exc.reason).model_dump(),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    reason = HTTP_REASONS.get(exc.status_code, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=error_response(reason).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    logger.warning("invalid_request path=%s", request.url.path)
    return JSONResponse(
        status_code=422,
        content=error_response("invalid_request").model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content=error_response("internal_error").model_dump(),
        # This handler runs in ServerErrorMiddleware, outside every user
        # middleware, so default_cache_control never sees this response.
        headers={"Cache-Control": NO_STORE},
    )


@app.get("/health", response_model=ApiSuccess[dict])
def health() -> ApiSuccess[dict]:
    return ok_response({"version": "0.1.0"})
