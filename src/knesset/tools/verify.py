"""Verifier tools: re-read cited chunks, re-run SQL, record a verdict."""

from knesset.tools.common import tool
from knesset.tools.research import get_chunks
from knesset.tools.sql import sql_query

__all__ = ["get_chunks", "rerun_sql", "verdict"]


@tool
def rerun_sql(sql: str) -> dict:
    """Run a query from a draft answer again, to check its numbers."""
    return sql_query(sql)


@tool
def verdict(status: str, issues: list[str] | None = None) -> dict:
    """The verifier's decision on a draft: pass, or fail with the issues to fix."""
    if status not in ("pass", "fail"):
        raise ValueError("status must be pass or fail")
    issues = [i.strip() for i in issues or [] if i.strip()]
    if status == "fail" and not issues:
        raise ValueError("a fail needs at least one issue")
    return {"status": status, "issues": issues, "evidence": []}
