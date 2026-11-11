# INFO: Internal ingest endpoints. Not used by the app clients: only the collector calls them.

import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ValidationError
from supabase import Client

from core.auth import require_ingest_token
from core.database import get_ingest_db
from core.exceptions import InvalidRequest
from models.ingest import (
    GenreIngestRequest,
    GenreIngestResult,
    ReleaseIngestRequest,
    ReleaseIngestResult,
)
from models.responses import ApiSuccess, ok_response
from services.ingest_service import ingest_genres, ingest_releases

logger = logging.getLogger(__name__)

_BodyT = TypeVar("_BodyT", bound=BaseModel)

# Internal: the front never calls these routes, only the collector does, with
# INGEST_TOKEN. The token dependency is router-level so every route here
# inherits it. The database client is get_ingest_db: the
# write is one long RPC and needs a read timeout longer than the app's.
router = APIRouter(
    prefix="/ingest",
    tags=["ingest"],
    dependencies=[Depends(require_ingest_token)],
)


def _body_reader(
    model: type[_BodyT],
) -> Callable[[Request], Coroutine[Any, Any, _BodyT]]:
    # The body is NOT a parameter of the route on purpose: FastAPI parses a
    # declared body before it resolves the dependencies, so a malformed JSON
    # would answer 422 to a caller without the token. As a dependency it runs
    # after the router-level token check, which means that without the token
    # the body is never read. A body that is not valid JSON, or does not fit
    # the model, is the same 422 invalid_request as any other, with no detail.
    async def read_body(request: Request) -> _BodyT:
        try:
            return model.model_validate_json(await request.body())
        except ValidationError as exc:
            logger.warning("ingest_invalid_body errors=%d", exc.error_count())
            raise InvalidRequest() from exc

    return read_body


read_genre_ingest_body = _body_reader(GenreIngestRequest)
read_release_ingest_body = _body_reader(ReleaseIngestRequest)


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    # OpenAPI resolves "$ref" from the document root, so the "$defs" that
    # pydantic emits next to the schema would dangle: the models are inlined.
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return _inline_refs(defs[ref.removeprefix("#/$defs/")], defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _body_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    return _inline_refs(schema, defs)


def _request_body(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _body_schema(model)}},
        }
    }


# The body is read in a dependency (see above), so FastAPI does not see it.
# openapi_extra puts it back in /docs and /openapi.json.
@router.post(
    "/genres",
    response_model=ApiSuccess[GenreIngestResult],
    openapi_extra=_request_body(GenreIngestRequest),
)
def ingest_genres_route(
    body: GenreIngestRequest = Depends(read_genre_ingest_body),  # noqa: B008
    db: Client = Depends(get_ingest_db),  # noqa: B008
) -> ApiSuccess[GenreIngestResult]:
    return ok_response(ingest_genres(db, body))


@router.post(
    "/releases",
    response_model=ApiSuccess[ReleaseIngestResult],
    openapi_extra=_request_body(ReleaseIngestRequest),
)
def ingest_releases_route(
    body: ReleaseIngestRequest = Depends(read_release_ingest_body),  # noqa: B008
    db: Client = Depends(get_ingest_db),  # noqa: B008
) -> ApiSuccess[ReleaseIngestResult]:
    return ok_response(ingest_releases(db, body))
