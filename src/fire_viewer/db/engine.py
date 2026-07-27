from collections.abc import Iterator

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from fire_viewer.core.config import Settings


def normalize_database_url(database_url: str) -> str:
    """Select psycopg 3 explicitly for PostgreSQL URLs supplied by Neon/Vercel."""

    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def create_db_engine(settings: Settings) -> Engine:
    database_url = normalize_database_url(settings.database_url)
    connect_args: dict[str, object] = {}
    engine_options: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args = {
            "check_same_thread": False,
            "timeout": settings.sqlite_busy_timeout_ms / 1_000,
        }
    else:
        engine_options = {
            "pool_size": settings.database_pool_size,
            "max_overflow": settings.database_max_overflow,
            "pool_recycle": settings.database_pool_recycle_seconds,
        }

    engine = create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        future=True,
        **engine_options,
    )

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
            cursor.close()

    if engine.dialect.name == "postgresql":
        statement_timeout_ms = settings.database_statement_timeout_ms

        @event.listens_for(engine, "begin")
        def set_postgres_transaction_settings(connection: Connection) -> None:
            # Neon pooler rejects statement_timeout as a startup parameter.
            # SET LOCAL is transaction-scoped and therefore safe with transaction pooling.
            connection.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
            connection.exec_driver_sql(f"SET LOCAL statement_timeout = {statement_timeout_ms}")

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
