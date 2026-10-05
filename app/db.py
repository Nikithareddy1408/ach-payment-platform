"""Database access: a connection pool, transactions, and migrations."""
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def create_pool(database_url: str, max_size: int = 20) -> ConnectionPool:
    # autocommit=True: single statements commit on their own; multi-statement
    # work uses transaction() below so it is all-or-nothing.
    return ConnectionPool(
        database_url,
        min_size=1,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": True, "application_name": "ach-payment-platform"},
        open=True,
    )


@contextmanager
def transaction(pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    """Everything done with `conn` inside this block is saved together, or not at all."""
    with pool.connection() as conn, conn.transaction():
        yield conn


def is_unique_violation(error: Exception) -> bool:
    return isinstance(error, psycopg.errors.UniqueViolation)


def migrate(pool: ConnectionPool, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Applies each .sql file in migrations/ once, in order. An advisory lock
    makes it safe even if several instances start at the same time."""
    lock_id = int(hashlib.sha256(b"ach-migrations").hexdigest()[:12], 16)
    applied: list[str] = []
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            done = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
    return applied
