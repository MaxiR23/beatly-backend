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


def ok_response(data: T) -> ApiSuccess[T]:
    return ApiSuccess(data=data)


def empty_response(reason: str) -> ApiError:
    return ApiError(reason=reason)


def error_response(reason: str) -> ApiError:
    return ApiError(reason=reason)