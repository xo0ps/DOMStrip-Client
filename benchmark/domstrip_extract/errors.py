"""Stable error contract for the conversion API.

Every failure that reaches an HTTP response is represented as an :class:`AppError`
carrying a stable ``code``, a sanitized ``message``, an HTTP ``status``, and a
``retryable`` flag. Expected failures retain their specific status instead of
being collapsed into a blanket 400.
"""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from typing import Any


class ErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    UNAUTHORIZED = "unauthorized"
    TARGET_BLOCKED = "target_blocked"
    ENTITLEMENT_REQUIRED = "entitlement_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    REQUEST_IN_PROGRESS = "request_in_progress"
    RESULT_EXPIRED = "result_expired"
    REQUEST_TOO_LARGE = "request_too_large"
    SOURCE_TOO_LARGE = "source_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONTENT_NOT_EXTRACTABLE = "content_not_extractable"
    OUTPUT_TOO_LARGE = "output_too_large"
    RATE_LIMITED = "rate_limited"
    CONCURRENCY_LIMITED = "concurrency_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    UPSTREAM_ERROR = "upstream_error"
    OVERLOADED = "overloaded"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    CONVERSION_TIMEOUT = "conversion_timeout"
    INTERNAL_ERROR = "internal_error"


_STATUS: dict[ErrorCode, HTTPStatus] = {
    ErrorCode.INVALID_JSON: HTTPStatus.BAD_REQUEST,
    ErrorCode.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    ErrorCode.TARGET_BLOCKED: HTTPStatus.FORBIDDEN,
    ErrorCode.ENTITLEMENT_REQUIRED: HTTPStatus.FORBIDDEN,
    ErrorCode.IDEMPOTENCY_CONFLICT: HTTPStatus.CONFLICT,
    ErrorCode.REQUEST_IN_PROGRESS: HTTPStatus.CONFLICT,
    ErrorCode.RESULT_EXPIRED: HTTPStatus.CONFLICT,
    ErrorCode.REQUEST_TOO_LARGE: HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    ErrorCode.SOURCE_TOO_LARGE: HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
    ErrorCode.UNSUPPORTED_ENCODING: HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
    ErrorCode.INVALID_REQUEST: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    ErrorCode.METHOD_NOT_ALLOWED: HTTPStatus.METHOD_NOT_ALLOWED,
    ErrorCode.CONTENT_NOT_EXTRACTABLE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.OUTPUT_TOO_LARGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    ErrorCode.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.CONCURRENCY_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.QUOTA_EXCEEDED: HTTPStatus.TOO_MANY_REQUESTS,
    ErrorCode.UPSTREAM_ERROR: HTTPStatus.BAD_GATEWAY,
    ErrorCode.OVERLOADED: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.DEPENDENCY_UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.UPSTREAM_TIMEOUT: HTTPStatus.GATEWAY_TIMEOUT,
    ErrorCode.CONVERSION_TIMEOUT: HTTPStatus.GATEWAY_TIMEOUT,
    ErrorCode.INTERNAL_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
}

# Codes that indicate the caller may retry the same logical request.
_RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.REQUEST_IN_PROGRESS,
        ErrorCode.RATE_LIMITED,
        ErrorCode.CONCURRENCY_LIMITED,
        ErrorCode.OVERLOADED,
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        ErrorCode.UPSTREAM_TIMEOUT,
        ErrorCode.CONVERSION_TIMEOUT,
        ErrorCode.INTERNAL_ERROR,
    }
)


class AppError(Exception):
    """A mapped, user-facing application error.

    ``details`` and ``headers`` never contain secrets, upstream bodies, or stack
    traces. ``retry_after`` is an optional integer number of seconds.
    """

    __slots__ = ("code", "details", "message", "retry_after", "retryable", "status")

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status: int,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details = details
        self.retry_after = retry_after

    @classmethod
    def make(
        cls,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> AppError:
        status = int(_STATUS[code])
        if retryable is None:
            retryable = code in _RETRYABLE
        return cls(
            code=code,
            message=message,
            status=status,
            retryable=retryable,
            details=details,
            retry_after=retry_after,
        )

    def as_body(self, request_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
            }
        }
        if request_id:
            body["error"]["request_id"] = request_id
        if self.details:
            body["error"]["details"] = self.details
        return body


# Convenience constructors -------------------------------------------------


def invalid_json() -> AppError:
    return AppError.make(ErrorCode.INVALID_JSON, "The request body is not valid JSON.")


def invalid_request(message: str) -> AppError:
    return AppError.make(ErrorCode.INVALID_REQUEST, message)


def unauthorized(message: str = "Missing or invalid credential.") -> AppError:
    return AppError.make(ErrorCode.UNAUTHORIZED, message)


def target_blocked(reason: str = "The destination is not permitted.") -> AppError:
    return AppError.make(ErrorCode.TARGET_BLOCKED, reason)


def entitlement_required() -> AppError:
    return AppError.make(ErrorCode.ENTITLEMENT_REQUIRED, "A paid entitlement is required.")


def upstream_error(message: str = "The source could not be fetched.") -> AppError:
    return AppError.make(ErrorCode.UPSTREAM_ERROR, message)


def upstream_timeout() -> AppError:
    return AppError.make(
        ErrorCode.UPSTREAM_TIMEOUT,
        "The source page did not respond within the allowed time.",
    )


def conversion_timeout() -> AppError:
    return AppError.make(
        ErrorCode.CONVERSION_TIMEOUT,
        "Content extraction exceeded the allowed time.",
    )


def internal_error() -> AppError:
    return AppError.make(
        ErrorCode.INTERNAL_ERROR,
        "An unexpected error occurred.",
    )
