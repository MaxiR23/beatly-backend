# INFO: Response envelope for all API endpoints.

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiSuccess(BaseModel, Generic[T]):
    ok: Literal[True] = True
    data: T


class ApiError(BaseModel):
    ok: Literal[False] = False
    reason: str


class PageBlock(BaseModel):
    limit: int
    next_cursor: str | None = None
    has_more: bool = False
    # Exact, and present only on the first page (a request without a
    # cursor). null afterwards — the client keeps the first value.
    total: int | None = None


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    page: PageBlock


def ok_response(data: T) -> ApiSuccess[T]:
    return ApiSuccess(data=data)


def empty_response(reason: str) -> ApiError:
    return ApiError(reason=reason)


def error_response(reason: str) -> ApiError:
    return ApiError(reason=reason)
