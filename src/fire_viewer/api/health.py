from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.orm import Session

from fire_viewer.api.dependencies import SessionDep, SettingsDep
from fire_viewer.domain.errors import DomainError
from fire_viewer.domain.schemas import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])

POSTGRES_SPATIAL_INDEXES = (
    "ix_incident_series_reference_geog_gist",
    "ix_incident_series_reference_geom_l93_gist",
    "ix_observation_geometry_geog_gist",
    "ix_observation_geometry_l93_gist",
)


def _database_revision(session: Session) -> str:
    revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    if not isinstance(revision, str):
        raise RuntimeError("Alembic revision is invalid")
    return revision


def _check_spatial_runtime(session: Session) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        exists = session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='incident_series_rtree'")
        ).scalar_one_or_none()
        if not exists:
            raise RuntimeError("SQLite RTree index is missing")
        return
    if dialect != "postgresql":
        raise RuntimeError(f"Unsupported database dialect: {dialect}")

    postgis = session.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
    ).scalar_one_or_none()
    if not postgis:
        raise RuntimeError("PostGIS extension is missing")
    for index_name in POSTGRES_SPATIAL_INDEXES:
        index = session.execute(
            text("SELECT to_regclass(:index_name)"),
            {"index_name": index_name},
        ).scalar_one_or_none()
        if index is None:
            raise RuntimeError(f"PostGIS index is missing: {index_name}")


@router.get("/healthz", response_model=HealthResponse, include_in_schema=False)
def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(status="ok", version=settings.app_version)


@router.get("/readyz", response_model=ReadinessResponse, include_in_schema=False)
def readiness(session: SessionDep, settings: SettingsDep) -> ReadinessResponse:
    try:
        session.execute(text("SELECT 1"))
        revision = _database_revision(session)
        if revision != settings.database_schema_revision:
            raise RuntimeError(
                f"Database revision {revision} does not match {settings.database_schema_revision}"
            )
        _check_spatial_runtime(session)
    except Exception as exc:
        raise DomainError(
            status_code=503,
            code="not_ready",
            title="Service not ready",
            detail="Database or spatial index readiness checks failed.",
        ) from exc
    return ReadinessResponse(
        status="ready",
        database="ok",
        schema_revision=revision,
        spatial_index="ok",
    )
