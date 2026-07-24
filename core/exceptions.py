# INFO: Domain exceptions and their HTTP mapping.

HTTP_REASONS = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    429: "rate_limited",
}


class AppError(Exception):
    status_code: int = 500
    reason: str = "internal_error"

    def __init__(self, reason: str | None = None) -> None:
        if reason is not None:
            self.reason = reason
        super().__init__(self.reason)


class NotFound(AppError):
    status_code = 404
    reason = "not_found"


class ResourceEmpty(AppError):
    status_code = 200
    reason = "empty"


class InvalidRequest(AppError):
    status_code = 422
    reason = "invalid_request"


class Unauthorized(AppError):
    status_code = 401
    reason = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    reason = "forbidden"


class Conflict(AppError):
    status_code = 409
    reason = "conflict"


class UpstreamError(AppError):
    status_code = 502
    reason = "upstream_error"


class UpstreamTimeout(AppError):
    status_code = 504
    reason = "upstream_timeout"
