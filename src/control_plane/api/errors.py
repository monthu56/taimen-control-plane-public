"""The single error envelope and exception -> HTTP mapping.

Every error leaves the API as::

    {"error": {"code": ..., "message": ..., "details": {...}, "requestId": ...}}

Stack traces never reach the client; unexpected errors are logged server-side.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from control_plane import observability
from control_plane.domain.errors import DomainError
from control_plane.logging import request_id_var

logger = logging.getLogger(__name__)


class BodyTooLargeError(Exception):
    pass


def error_body(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "requestId": request_id or request_id_var.get() or "unknown",
        }
    }


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(code, message, details=details))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        if exc.code in ("stale_claim", "run_not_active"):
            observability.inc("stale_fencing_rejections_total")
        return error_response(exc.http_status, exc.code, exc.message, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "loc": ".".join(str(part) for part in err.get("loc", ())),
                "message": err.get("msg", "invalid"),
            }
            for err in exc.errors()[:20]
        ]
        return error_response(
            400,
            "invalid_request",
            "Request does not match the API contract",
            details={"errors": errors},
        )

    @app.exception_handler(BodyTooLargeError)
    async def handle_body_too_large(request: Request, exc: BodyTooLargeError) -> JSONResponse:
        return error_response(413, "request_too_large", "Request body exceeds the size limit")

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "not_found",
            405: "method_not_allowed",
            429: "rate_limited",
            503: "unavailable",
        }.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        return error_response(500, "internal_error", "Internal server error")
