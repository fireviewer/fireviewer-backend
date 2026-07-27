from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Info, make_asgi_app
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fire_viewer.api.admin_v2 import router as admin_v2_router
from fire_viewer.api.agent_batches import router as agent_batches_router
from fire_viewer.api.errors import install_exception_handlers
from fire_viewer.api.health import router as health_router
from fire_viewer.api.middleware import (
    AdminNoStoreMiddleware,
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    TraceMiddleware,
)
from fire_viewer.api.router import api_router
from fire_viewer.core.config import Settings, get_settings
from fire_viewer.core.logging import configure_logging
from fire_viewer.core.security import JwtVerifier
from fire_viewer.db.engine import create_db_engine, create_session_factory

BUILD_INFO = Info("fire_viewer_build", "Fire-Viewer API build metadata")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Transactional incident registry with idempotent ingestion, conservative spatial "
            "matching, immutable audit events and viewer manifests."
        ),
        openapi_url="/openapi.json",
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.jwt_verifier = JwtVerifier(settings)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "If-None-Match",
                "Idempotency-Key",
                "X-Trace-Id",
                "X-CSRF-Token",
                "X-Blob-Upload-Grant",
            ],
            expose_headers=["ETag", "Idempotent-Replay", "X-Trace-Id"],
            max_age=600,
        )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_body_bytes,
    )
    app.add_middleware(TraceMiddleware)
    app.add_middleware(AdminNoStoreMiddleware, api_prefix=settings.api_prefix)
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.environment in {"staging", "production"},
    )

    install_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)
    app.include_router(admin_v2_router)
    app.include_router(agent_batches_router)
    app.mount("/metrics", make_asgi_app())

    default_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = default_openapi()
        manifest_operation = (
            schema.get("paths", {})
            .get(f"{settings.api_prefix}/incident/{{fire_id}}/manifest", {})
            .get("get")
        )
        if manifest_operation:
            # FireIdDep converts the only path validation failure to the documented 400 response.
            manifest_operation.get("responses", {}).pop("422", None)
        return schema

    # FastAPI exposes this generator as a method, but per-instance replacement is its
    # supported extension point for removing the inapplicable automatic 422 response.
    app.openapi = openapi  # type: ignore[method-assign]

    BUILD_INFO.info({"version": settings.app_version, "environment": settings.environment})
    return app


app = create_app()
