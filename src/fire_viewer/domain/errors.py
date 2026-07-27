from typing import Any


class DomainError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.extra = extra or {}
        self.headers = headers or {}


class NotFoundError(DomainError):
    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            status_code=404,
            code="not_found",
            title="Resource not found",
            detail=f"{resource} '{identifier}' does not exist.",
        )


class ConflictError(DomainError):
    def __init__(self, code: str, detail: str, *, extra: dict[str, Any] | None = None) -> None:
        super().__init__(
            status_code=409,
            code=code,
            title="Conflict",
            detail=detail,
            extra=extra,
        )


class ForbiddenError(DomainError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=403,
            code="forbidden",
            title="Forbidden",
            detail=detail,
        )


class UnauthorizedError(DomainError):
    def __init__(self, detail: str = "A valid bearer token is required.") -> None:
        super().__init__(
            status_code=401,
            code="unauthorized",
            title="Unauthorized",
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


class SourceUnauthorizedError(DomainError):
    def __init__(self, detail: str = "A valid source credential is required.") -> None:
        super().__init__(
            status_code=401,
            code="source_unauthorized",
            title="Source authentication required",
            detail=detail,
            headers={"WWW-Authenticate": "SourceToken"},
        )


class BadRequestError(DomainError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(
            status_code=400,
            code=code,
            title="Bad request",
            detail=detail,
        )


class PayloadTooLargeError(DomainError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=413,
            code="payload_too_large",
            title="Payload too large",
            detail=detail,
        )
