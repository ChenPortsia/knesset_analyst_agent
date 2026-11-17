"""Notebook helpers. More (show_doc, search, ask, ...) arrive with their phases."""

import os
from pathlib import Path

import pandas as pd

from knesset.db import connect
from knesset.tracing import trace_link

__all__ = ["q", "runs", "failures", "trace_link"]

# Notebooks run from notebooks/; work from the repo root so .env and logs/ resolve.
if Path.cwd().name == "notebooks":
    os.chdir(Path.cwd().parent)


def q(sql: str, params: tuple | dict | None = None, user: str = "etl") -> pd.DataFrame:
    with connect(user) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description] if cur.description else []
        return pd.DataFrame(cur.fetchall(), columns=cols)


def runs(step: str | None = None, n: int = 20) -> pd.DataFrame:
    return q(
        "select * from ops.runs where %(step)s::text is null or step = %(step)s"
        " order by started_at desc limit %(n)s",
        {"step": step, "n": n},
    )


def failures(step: str | None = None, n: int = 20) -> pd.DataFrame:
    return q(
        "select * from ops.failures where %(step)s::text is null or step = %(step)s"
        " order by created_at desc limit %(n)s",
        {"step": step, "n": n},
    )
