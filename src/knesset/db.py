"""Database connections and migrations."""

from pathlib import Path

import psycopg

from knesset.config import settings

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def connect(user: str = "etl", **kwargs) -> psycopg.Connection:
    return psycopg.connect(settings().dsn(user), connect_timeout=5, **kwargs)


def migrate() -> list[str]:
    """Apply sql/NNN_*.sql files not yet recorded in ops.migrations (000_init runs at initdb)."""
    applied = []
    with connect() as conn:
        conn.execute(
            "create table if not exists ops.migrations"
            " (name text primary key, applied_at timestamptz not null default now())"
        )
        done = {r[0] for r in conn.execute("select name from ops.migrations")}
        for f in sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            if f.name.startswith("000_") or f.name in done:
                continue
            conn.execute(f.read_text())
            conn.execute("insert into ops.migrations (name) values (%s)", (f.name,))
            applied.append(f.name)
    return applied
