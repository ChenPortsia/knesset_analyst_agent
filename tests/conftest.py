import psycopg
import pytest

from knesset.config import Settings, settings
from knesset.db import migrate

TEST_DB = "knesset_test"


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Logs go to a temp dir; Phoenix points at a closed port; no traces leave for Langfuse
    (the repo's .env may hold real keys)."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PHOENIX_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def db(monkeypatch):
    """A fresh, throwaway knesset_test database (never the real one), with migrations applied.

    Skips when Postgres is not reachable (start it with `make up`).
    """
    s = Settings()
    admin = f"postgresql://postgres:{s.postgres_password}@{s.db_host}:{s.db_port}"
    try:
        conn = psycopg.connect(f"{admin}/postgres", autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("postgres not reachable")
    with conn:
        conn.execute(f"drop database if exists {TEST_DB} with (force)")
        conn.execute(f"create database {TEST_DB} owner etl")
    with psycopg.connect(f"{admin}/{TEST_DB}", autocommit=True) as conn:
        conn.execute("create extension vector; create extension pg_trgm")
        for schema in ("raw", "ops", "text", "marts"):
            conn.execute(f"create schema {schema} authorization etl")
        # as in sql/000_init.sql: agent_ro reads marts and text (tables created by etl)
        conn.execute("grant usage on schema marts, text to agent_ro")
        conn.execute(
            "alter default privileges for role etl in schema marts, text"
            " grant select on tables to agent_ro"
        )
    monkeypatch.setenv("DB_NAME", TEST_DB)
    settings.cache_clear()
    migrate()
    yield
