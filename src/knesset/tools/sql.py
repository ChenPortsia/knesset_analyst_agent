"""Analyst tools: the marts schema, read-only SQL, and chart specs."""

import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from knesset.db import connect
from knesset.tools.common import jsonable, tool

SCHEMA = Path(__file__).resolve().parents[3] / "dbt" / "models" / "marts" / "schema.yml"
ROW_LIMIT = 200
CHART_KINDS = {"bar", "line", "pie", "scatter"}


@cache
def models() -> list[dict]:
    return yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))["models"]


@tool
def describe_schema(topic: str | None = None) -> dict:
    """The marts tables (schema `marts`, bilingual descriptions). With a topic (e.g. "votes",
    "הצבעות", "fct_bill"), the matching tables with their columns."""
    words = [w.lower() for w in (topic or "").split()]
    tables = []
    for m in models():
        cols = m.get("columns", [])
        haystack = " ".join([m["name"], m.get("description", "")] + [c["name"] for c in cols])
        if not words:
            tables.append({"table": f"marts.{m['name']}", "description": m.get("description")})
        elif any(w in haystack.lower() for w in words):
            tables.append(
                {
                    "table": f"marts.{m['name']}",
                    "description": m.get("description"),
                    "columns": {c["name"]: c.get("description") for c in cols},
                }
            )
    return {"tables": tables, "evidence": []}


def check_select(sql: str) -> str:
    """One SELECT (or WITH … SELECT) statement, without a trailing semicolon."""
    sql = sql.strip().rstrip(";").strip()
    if ";" in re.sub(r"'[^']*'", "", sql):
        raise ValueError("one statement only")
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise ValueError("only SELECT queries are allowed")
    return sql


@tool
def sql_query(sql: str) -> dict:
    """Run one read-only SELECT on the marts (and text) schemas as agent_ro: 15 s timeout, at
    most 200 rows returned. Returns columns, rows and whether the result was cut."""
    sql = check_select(sql)
    with connect("agent_ro") as conn, conn.cursor() as cur:
        cur.execute(f"select * from ({sql}) as q limit {ROW_LIMIT + 1}")
        cols = [c.name for c in cur.description]
        data = [jsonable(list(r)) for r in cur.fetchall()]
    return {
        "columns": cols,
        "rows": data[:ROW_LIMIT],
        "row_count": min(len(data), ROW_LIMIT),
        "truncated": len(data) > ROW_LIMIT,
        "evidence": [{"type": "sql", "ref": sql}],
    }


@tool
def make_chart(
    columns: list[str],
    rows: list[list[str | float | None]],
    kind: str,
    x: str,
    y: str,
    series: str | None = None,
    title: str = "",
) -> dict:
    """A Plotly figure spec for the UI from a sql_query result (its `columns` and `rows`):
    kind (bar, line, pie, scatter), x and y (column names), optional series (a column), title.
    Typed arguments, not free-form objects: OpenAI models reject open object schemas."""
    if kind not in CHART_KINDS:
        raise ValueError(f"kind must be one of {sorted(CHART_KINDS)}")
    for name in filter(None, (x, y, series)):
        if name not in columns:
            raise ValueError(f"no column {name!r}; columns: {columns}")
    records = [dict(zip(columns, r, strict=True)) for r in rows]
    groups: dict[Any, list[dict]] = {}
    for r in records:
        groups.setdefault(r[series] if series else y, []).append(r)
    traces = []
    for name, rs in groups.items():
        if kind == "pie":
            traces.append(
                {"type": "pie", "labels": [r[x] for r in rs], "values": [r[y] for r in rs]}
            )
        else:
            trace = {"type": "bar" if kind == "bar" else "scatter", "name": str(name)}
            trace |= {"x": [r[x] for r in rs], "y": [r[y] for r in rs]}
            if kind != "bar":
                trace["mode"] = "lines+markers" if kind == "line" else "markers"
            traces.append(trace)
    layout = {"title": {"text": title}, "xaxis": {"title": {"text": x}},
              "yaxis": {"title": {"text": y}}}  # fmt: skip
    return {"chart": {"data": traces, "layout": layout}, "evidence": []}
