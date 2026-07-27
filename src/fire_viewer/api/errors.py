from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from fire_viewer.core.context import trace_id_var
from fire_viewer.core.ids import new_trace_id
from fire_viewer.domain.errors import DomainError

logger = logging.getLogger("fire_viewer.errors")


def _problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    extra: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", None) or trace_id_var.get() or new_trace_id()
    payload: dict[str, Any] = {
        "type": f"urn:fire-viewer:error:{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "trace_id": trace_id,
    }
    if extra:
        payload.update(extra)
    response_headers = {"X-Trace-Id": trace_id, **(headers or {})}
    return JSONResponse(
        payload,
        status_code=status,
        media_type="application/problem+json",
        headers=response_headers,
    )


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return _problem(
            request,
            status=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            extra=exc.extra,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
                "type": item["type"],
            }
            for item in exc.errors()
        ]
        return _problem(
            request,
            status=422,
            code="validation_error",
            title="Request validation failed",
            detail="One or more request fields are invalid.",
            extra={"errors": errors},
        )

    @app.exception_handler(OperationalError)
    async def database_operational_error_handler(
        request: Request, exc: OperationalError
    ) -> JSONResponse:
        logger.exception("database_operational_error")
        detail = "The database is temporarily unavailable."
        headers: dict[str, str] = {}
        if "locked" in str(exc).casefold():
            detail = "The SQLite writer is busy; retry the request shortly."
            headers["Retry-After"] = "1"
        return _problem(
            request,
            status=503,
            code="database_unavailable",
            title="Service temporarily unavailable",
            detail=detail,
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(
            request,
            status=exc.status_code,
            code="http_error",
            title="HTTP error",
            detail=str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", exc_info=exc)
        return _problem(
            request,
            status=500,
            code="internal_error",
            title="Internal server error",
            detail="An unexpected error occurred. Refer to trace_id when reporting it.",
        )
