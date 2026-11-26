"""The semantic layer (semantic.yaml): instructions for each agent, and governed metrics
compiled to SQL. A metric is defined once here, so every agent counts the same way and the
SQL it cites can be re-run by the verifier."""

from functools import cache
from pathlib import Path
from typing import Any

import yaml

LAYER = Path(__file__).with_name("semantic.yaml")
MAX_ROWS = 200
# Which parts of the layer each agent reads in its prompt.
PARTS = {
    "orchestrator": ("glossary", "routes", "metric_names"),
    "analyst": ("glossary", "entities", "metrics"),
    "researcher": ("glossary", "routes", "text_entities"),
    "summarizer": ("glossary", "text_entities"),
    "verifier": ("glossary",),
    "draft": ("glossary",),
}
TEXT_ENTITIES = ("document", "chunk", "section", "participant", "mention")


@cache
def layer() -> dict:
    return yaml.safe_load(LAYER.read_text(encoding="utf-8"))


def dims(name: str) -> dict[str, dict]:
    """A metric's dimensions as {expr, key, join}, with month and year from its date."""
    m = layer()["metrics"][name]
    out = {d: v if isinstance(v, dict) else {"expr": v} for d, v in m["dims"].items()}
    if m.get("date"):
        out["month"] = {"expr": f"to_char({m['date']}, 'YYYY-MM')"}
        out["year"] = {"expr": f"extract(year from {m['date']})::int"}
    return out


def compile_metric(
    name: str, by: list[str] | None = None, filters: dict[str, Any] | None = None, limit: int = 25
) -> tuple[str, dict]:
    """The SQL and parameters for a metric grouped `by` dimensions and filtered by dimension
    values (ids for dimensions with a key) and date_from / date_to."""
    metrics = layer()["metrics"]
    if name not in metrics:
        raise ValueError(f"unknown metric {name!r}; metrics: {sorted(metrics)}")
    m, d = metrics[name], dims(name)
    by, filters = list(by or []), dict(filters or {})
    bad = [x for x in by + [f for f in filters if f not in ("date_from", "date_to")] if x not in d]
    if bad:
        raise ValueError(f"{name} has no dimension {bad}; dimensions: {sorted(d)}")
    if (filters.get("date_from") or filters.get("date_to")) and not m.get("date"):
        raise ValueError(f"{name} has no date to filter on")
    joins = dict.fromkeys(d[x]["join"] for x in by + list(filters) if d.get(x, {}).get("join"))
    where, params = [m.get("where") or "true"], {}
    for f, value in filters.items():
        if f == "date_from":
            where.append(f"{m['date']} >= %(date_from)s")
        elif f == "date_to":
            where.append(f"{m['date']} < %(date_to)s::date + 1")
        else:
            value = value if isinstance(value, list) else [value]
            where.append(f"{d[f].get('key', d[f]['expr'])} = any(%({f})s)")
        params[f] = value
    select = [f"{d[x]['expr']} as {x}" for x in by] + [f"{m['value']} as {name}"]
    sql = (
        f"select {', '.join(select)} from {m['from']} {' '.join(joins)} where {' and '.join(where)}"
    )
    if by:
        timeline = [str(i + 1) for i, x in enumerate(by) if x in ("month", "year")]
        order = ", ".join(timeline) if timeline else f"{len(by) + 1} desc"
        sql += f" group by {', '.join(str(i + 1) for i in range(len(by)))} order by {order}"
        sql += f" limit {min(max(limit, 1), MAX_ROWS)}"
    return " ".join(sql.split()), params


def brief(agent: str) -> str:
    """The parts of the layer for one agent's prompt, as short Markdown."""
    parts = PARTS.get(agent, ())
    if not parts:
        return ""
    lay, out = layer(), ["## The data (semantic layer)"]
    if "glossary" in parts:
        out.append("Question words and what they mean in the data:")
        out += [f"- {' / '.join(g['terms'])}: {g['means']}" for g in lay["glossary"]]
    if "routes" in parts:
        out.append("\nWhich agent and tool answers what:")
        out += [f"- {r['ask']} → {r['use']}" for r in lay["routes"]]
    if "metric_names" in parts:
        out.append("\nMetrics the analyst computes directly: " + ", ".join(lay["metrics"]))
    entities = lay["entities"]
    if "text_entities" in parts:
        entities = {k: v for k, v in entities.items() if k in TEXT_ENTITIES}
    if "entities" in parts or "text_entities" in parts:
        out.append("\nTables:")
        for name, e in entities.items():
            out.append(f"- **{name}** `{e['table']}` (key {e['key']}): {e['columns']}")
            out += [f"  - {n}" for n in e.get("notes", [])]
    if "metrics" in parts:
        out.append(
            "\nMetrics for the `metric` tool (name: what it counts; dimensions for by/filters):"
        )
        for name, m in lay["metrics"].items():
            out.append(f"- `{name}`: {m['describe']}; dims {', '.join(dims(name))}")
    return "\n".join(out)
