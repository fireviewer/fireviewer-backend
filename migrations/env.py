import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from fire_viewer.db import models  # noqa: F401
from fire_viewer.db.base import Base
from fire_viewer.db.engine import normalize_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# The agent runtime owns these private execution structures on the ``ia`` branch.
# They remain in the shared Alembic history so a public-backend deployment can
# follow a database that has already processed agent work, but they are not part
# of the public backend SQLAlchemy metadata and must not be proposed for removal.
EXTERNAL_AGENT_TABLES = frozenset(
    {
        "agent_consensus_result",
        "agent_model_candidate_run",
        "agent_stage_run",
    }
)
EXTERNAL_AGENT_COLUMNS = {
    "agent_situation_report_revision": frozenset(
        {"published_at", "published_by", "publication_reason"}
    )
}


def database_url() -> str:
    """Resolve the Alembic target with the same FV_ override as the app."""

    raw_url = os.environ.get("FV_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    return normalize_database_url(raw_url)


def include_object(
    _object: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    del reflected, compare_to
    if type_ == "table" and name:
        return not (name.startswith("incident_series_rtree") or name in EXTERNAL_AGENT_TABLES)
    if type_ in {"column", "index"} and name:
        table_name = getattr(getattr(_object, "table", None), "name", None)
        if table_name == "agent_situation_report_revision" and type_ == "index":
            return name != "ix_agent_situation_report_revision_published_at"
        return name not in EXTERNAL_AGENT_COLUMNS.get(table_name, frozenset())
    return True


def run_migrations_offline() -> None:
    url = database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=url.startswith("sqlite"),
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = dict(config.get_section(config.config_ini_section, {}))
    configuration["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
