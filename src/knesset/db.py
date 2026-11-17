"""Database connections."""

import psycopg

from knesset.config import settings


def connect(user: str = "etl", **kwargs) -> psycopg.Connection:
    return psycopg.connect(settings().dsn(user), connect_timeout=5, **kwargs)
