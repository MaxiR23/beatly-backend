import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.exceptions import HTTP_REASONS, AppError
from core.logging import setup_logging
from models.responses import ApiSuccess, error_response, ok_response

setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="Beatly API")


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
    )


@app.get("/health", response_model=ApiSuccess[dict])
def health() -> ApiSuccess[dict]:
    return ok_response({"version": "0.1.0"})
