from sqlalchemy import text
from sqlalchemy.orm import Session


def begin_write_transaction(session: Session) -> None:
    """Start a write transaction.

    SQLite uses BEGIN IMMEDIATE to serialize its single writer before matching and ID allocation.
    PostgreSQL and other engines use the normal transaction semantics.
    """

    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    else:
        session.begin()
