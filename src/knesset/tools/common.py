"""Shared by all agents: the tool decorator, truncation, and resolve_entity."""

import functools
import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import structlog

from knesset.db import connect
from knesset.tracing import tracer

TEXT_LIMIT = 800  # characters of a hit chunk shown to an agent; neighbors get less
log = structlog.get_logger()


def truncate(text: str | None, limit: int = TEXT_LIMIT) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def jsonable(value: Any) -> Any:
    """Decimals as floats; dates and the like as strings, so results are JSON."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return str(value)


def tool(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Log and trace a tool call: name, arguments, time, evidence count, error."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        started = time.perf_counter()
        call = {"tool": fn.__name__, "args": json.dumps(jsonable(kwargs), ensure_ascii=False)[:500]}
        with tracer().start_as_current_span(f"tool.{fn.__name__}") as span:
            span.set_attribute("tool.args", call["args"])
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                log.warning("tool_call", **call, error=repr(e)[:300])
                span.set_attribute("tool.error", repr(e)[:300])
                raise
            ms = round((time.perf_counter() - started) * 1000)
            n = len(result.get("evidence", []))
            span.set_attribute("tool.ms", ms)
            span.set_attribute("tool.evidence", n)
            log.info("tool_call", **call, ms=ms, evidence=n)
            return result

    return wrapper


def rows(sql: str, params: dict | None = None) -> list[dict]:
    with connect("agent_ro") as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, jsonable(list(r)), strict=True)) for r in cur.fetchall()]


RESOLVE_SQL = """
with q as (select %(name)s::text as name, '%%' || replace(%(name)s, ' ', '%%') || '%%' as pat)
select * from (
    select 'person' as type, p.person_id as id, p.full_name as name,
           p.last_knesset as knesset,
           case when q.name = any(p.name_variants) or q.name = p.full_name then 1.0
                else length(q.name)::real / length(p.full_name) end as score
    from marts.dim_person p, q
    where (p.full_name ilike q.pat or exists (select 1 from unnest(p.name_variants) v
                                               where v ilike q.pat))
      and (%(k)s::int is null or %(k)s = any(p.mk_knessets))
    union all
    select 'faction', f.faction_id, f.faction_name, f.knesset_num,
           case when f.faction_name = q.name then 1.0
                else length(q.name)::real / length(f.faction_name) end
    from marts.dim_faction f, q
    where f.faction_name ilike q.pat and (%(k)s::int is null or f.knesset_num = %(k)s)
    union all
    select 'committee', c.committee_id, c.committee_name, c.knesset_num,
           case when c.committee_name = q.name then 1.0
                else length(q.name)::real / length(c.committee_name) end
    from marts.dim_committee c, q
    where c.committee_name ilike q.pat and (%(k)s::int is null or c.knesset_num = %(k)s)
    union all
    select 'ministry', m.ministry_id, m.ministry_name, null,
           case when m.ministry_name = q.name then 1.0
                else length(q.name)::real / length(m.ministry_name) end
    from marts.dim_ministry m, q
    where m.ministry_name ilike q.pat
    union all
    select 'bill', b.bill_id, b.bill_name, b.knesset_num,
           case when b.bill_name = q.name then 1.0
                else length(q.name)::real / length(b.bill_name) end
    from marts.fct_bill b, q
    where b.bill_name ilike q.pat and (%(k)s::int is null or b.knesset_num = %(k)s)
) t
where %(types)s::text[] is null or type = any(%(types)s)
order by score desc, knesset desc nulls last
limit %(limit)s
"""


@tool
def resolve_entity(
    name: str, types: list[str] | None = None, knesset: int | None = None, limit: int = 8
) -> dict:
    """Map a name to ids: person, faction, committee, ministry or bill. Every word of the name
    must appear (in order); exact names score 1. Use the ids in search and SQL filters."""
    name = " ".join(name.split())
    found = rows(RESOLVE_SQL, {"name": name, "types": types, "k": knesset, "limit": limit})
    for r in found:
        r["score"] = round(r["score"], 2)
    return {"candidates": found, "evidence": []}
