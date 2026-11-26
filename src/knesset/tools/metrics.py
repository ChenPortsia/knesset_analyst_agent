"""Tools on the semantic layer: governed metrics, a bill's votes, attendance lists, and
arithmetic the code can check. Each returns the exact SQL (or calculation) it used as
evidence, so the verifier — or the code check — can confirm every number."""

import ast
import operator
import re
from typing import Any

from knesset.semantic import compile_metric
from knesset.tools.common import query, tool

STAGES = [  # (pattern in the vote's title or subject, stage); first match wins
    (r"שלישית", "third"),
    (r"ראשונה", "first"),
    (r"טרומית", "preliminary"),
    (r"שנייה|סעיף|הסתייגו", "second"),
]
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.USub: operator.neg}  # fmt: skip


@tool
def metric(
    name: str, by: list[str] | None = None, filters: dict[str, Any] | None = None, limit: int = 25
) -> dict:
    """A governed metric from the semantic layer (bills, bills_passed, committee_sessions,
    plenum_sessions, votes, mk_votes, queries, words_spoken, attendance), grouped `by`
    dimensions (e.g. ["committee"], ["month"]) and filtered by dimension values or ids, e.g.
    {"knesset": [25], "committee": [4186], "date_from": "2025-01-01", "date_to": "2025-12-31"}."""
    sql, params = compile_metric(name, by, filters, limit)
    rows, literal = query(sql, params)
    return {"metric": name, "rows": rows, "evidence": [{"type": "sql", "ref": literal}]}


def stage(vote: dict) -> str:
    text = f"{vote.get('vote_title') or ''} {vote.get('vote_subject') or ''}"
    return next((s for pattern, s in STAGES if re.search(pattern, text)), "other")


@tool
def bill_votes(bill_id: int) -> dict:
    """Every plenum vote on a bill in time order, each with its reading stage (inferred from the
    title: preliminary, first, second = sections and reservations, third = final), and how
    each faction voted in the final vote (the last third-reading vote, else the last vote)."""
    votes, votes_sql = query(
        """select v.vote_id, v.vote_datetime, ps.knesset_num, v.vote_title, v.vote_subject,
                  v.vote_status_desc, v.n_for, v.n_against, v.n_abstain
           from marts.fct_vote v join marts.fct_plenum_session ps using (plenum_session_id)
           where v.item_id = %(b)s order by v.vote_datetime, v.ordinal""",
        {"b": bill_id},
    )
    if not votes:
        return {"bill_id": bill_id, "votes": [], "evidence": [{"type": "sql", "ref": votes_sql}]}
    for v in votes:
        v["stage"] = stage(v)
    third = [v for v in votes if v["stage"] == "third"]
    final = (third or votes)[-1]
    by_faction, faction_sql = query(
        """select faction_name, result_desc, count(*) as mks from marts.fct_vote_result
           where vote_id = %(v)s and result_desc in ('בעד', 'נגד', 'נמנע')
           group by 1, 2 order by 1, 3 desc""",
        {"v": final["vote_id"]},
    )
    second = [v for v in votes if v["stage"] == "second"]
    shown = [v for v in votes if v["stage"] != "second"]  # sections and reservations: counted
    return {
        "bill_id": bill_id,
        "n_votes": len(votes),
        "second_reading_votes": {"count": len(second), "first": second[0]["vote_datetime"],
                                 "last": second[-1]["vote_datetime"]} if second else None,
        "votes": shown[:30],
        "final_vote": {"vote_id": final["vote_id"], "stage": final["stage"],
                       "n_for": final["n_for"], "n_against": final["n_against"],
                       "n_abstain": final["n_abstain"], "by_faction": by_faction},
        "evidence": [{"type": "sql", "ref": votes_sql}, {"type": "sql", "ref": faction_sql}],
    }  # fmt: skip


@tool
def top_participants(
    by: str = "organization", filters: dict[str, Any] | None = None, limit: int = 25
) -> dict:
    """Who attended most, from the protocols' attendance lists: sessions per `by` value
    (organization, person or group = the list heading, e.g. מוזמנים). Filters as in `metric`
    (knesset, committee ids, group, date_from, date_to). Use it for "which organizations
    appeared" — mentions in speech are a different thing."""
    if by not in ("organization", "person", "group"):
        raise ValueError("by must be organization, person or group")
    result = metric("attendance", [by], filters, limit)
    return {"by": by, "rows": result["rows"], "evidence": result["evidence"]}


def evaluate(node: ast.AST, values: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return evaluate(node.body, values)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise ValueError(f"unknown name {node.id!r}; give it in values")
        return values[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](evaluate(node.left, values), evaluate(node.right, values))
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](evaluate(node.operand, values))
    raise ValueError("only numbers, names from values, + - * / and parentheses are allowed")


def plain(x: float) -> str:
    """1234.0 → '1234', 4.677 → '4.677' (no exponent: the code check reads these numbers)."""
    return str(int(x)) if float(x).is_integer() else str(x)


@tool
def compute(expression: str, values: dict[str, float]) -> dict:
    """Arithmetic on numbers you found, e.g. expression "100 * part / total" with values
    {"part": 371397, "total": 7940859}. Cite the result: the code checks it, not the model."""
    try:
        result = evaluate(ast.parse(expression, mode="eval"), values)
    except ZeroDivisionError as e:
        raise ValueError("division by zero") from e
    result = round(result, 4)
    shown = ", ".join(f"{k}={plain(v)}" for k, v in values.items())
    ref = f"{expression} with {shown} = {plain(result)}"
    return {"result": result, "evidence": [{"type": "calc", "ref": ref}]}
