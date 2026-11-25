"""The eval set and the model matrix: the same questions (eval/questions.yaml) under several
model configurations (eval/configs.yaml), graded in code, with cost and time per answer.

SQL questions are right when the answer has every value of the reference query's first row,
computed from the database at run time. Text questions score the share of expected term groups
the answer contains. Every answer is written to a JSONL file as it arrives; a summary per
configuration is printed and saved as Markdown next to it."""

import json
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from knesset.agents.graph import ask, numbers
from knesset.agents.models import ModelFactory, factory
from knesset.config import ROOT, settings
from knesset.tools.common import rows

QUESTIONS = ROOT / "eval" / "questions.yaml"
CONFIGS = ROOT / "eval" / "configs.yaml"


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def truth(sql: str) -> list[str]:
    """The reference query's first row, as strings (numbers normalized like the answer's)."""
    (row,) = rows(sql)[:1]
    return [str(v) for v in row.values()]


def grade(q: dict, answer: str, sources: list[dict], expected: list[str] | None = None) -> dict:
    """Score 0–1, and whether the answer cites a source."""
    cited = bool(sources) and "[1]" in answer
    low = answer.lower()
    if q["kind"] == "sql":
        found = numbers(answer)
        hits = [v in found if v.isdigit() else v.lower() in low for v in expected or []]
        missed = [v for v, hit in zip(expected or [], hits, strict=True) if not hit]
        return {"score": float(all(hits) and bool(hits)), "missed": missed, "cited": cited}
    groups = [any(t.lower() in low for t in g) for g in q["expect"]]
    missed = [g[0] for g, hit in zip(q["expect"], groups, strict=True) if not hit]
    return {"score": sum(groups) / len(groups), "missed": missed, "cited": cited}


def run(
    configs: list[str],
    max_usd: float,
    ids: list[str] | None = None,
    models: Callable[[dict[str, str]], ModelFactory] = factory,
    out: Path | None = None,
) -> Iterator[dict]:
    """Every question under every configuration, while the worst case fits under `max_usd`.
    Yields one graded row per answer and appends it to `out` (JSONL)."""
    s = settings()
    questions = [q for q in load(QUESTIONS) if not ids or q["id"] in ids]
    matrix = load(CONFIGS)
    expected = {q["id"]: truth(q["sql"]) for q in questions if q["kind"] == "sql"}
    out = out or s.log_dir / "eval" / f"{datetime.now():%Y-%m-%d_%H%M}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    spent = 0.0
    for name in configs:
        names = matrix[name] or {}
        for q in questions:
            if spent + s.agent_max_cost_usd > max_usd:
                yield {"config": name, "id": q["id"], "skipped": f"${spent:.2f} spent"}
                continue
            *_, (_, result) = ask(q["question"], user_id=f"eval-{name}", models=models(names),
                                  filters={"knessets": [25, 26]})  # fmt: skip
            u = result["usage"]
            spent += u["cost_usd"]
            answer = result["answer"] or ""
            row = {
                "config": name, "id": q["id"], "kind": q["kind"], "question": q["question"],
                **grade(q, answer, result["sources"], expected.get(q["id"])),
                "expected": expected.get(q["id"]) or q.get("expect"),
                "verdict": (result["verdict"] or {}).get("status"),
                "cost_usd": round(u["cost_usd"], 4), "calls": u["calls"], "steps": u["steps"],
                "seconds": u["seconds"], "trace_id": result["trace_id"], "answer": answer,
                "sources": result["sources"], "models": names,
            }  # fmt: skip
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            yield row


def summary(results: list[dict]) -> list[dict]:
    """One line per configuration: SQL right, text score, cited, cost and time."""
    out = []
    for name in dict.fromkeys(r["config"] for r in results):
        done = [r for r in results if r["config"] == name and "score" in r]
        if not done:
            continue
        sql = [r["score"] for r in done if r["kind"] == "sql"]
        text = [r["score"] for r in done if r["kind"] == "text"]
        out.append({
            "config": name,
            "answered": len(done),
            "sql_right": f"{sum(sql):.0f}/{len(sql)}",
            "text_score": round(sum(text) / len(text), 2) if text else None,
            "cited": f"{sum(r['cited'] for r in done)}/{len(done)}",
            "cost_usd": round(sum(r["cost_usd"] for r in done), 3),
            "usd_per_q": round(sum(r["cost_usd"] for r in done) / len(done), 3),
            "s_per_q": round(sum(r["seconds"] for r in done) / len(done), 1),
        })  # fmt: skip
    return out


def table(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    head = "| " + " | ".join(rows[0]) + " |"
    return [head, "|" + "---|" * len(rows[0])] + [
        "| " + " | ".join(str(v) for v in r.values()) + " |" for r in rows
    ]


def markdown(results: list[dict]) -> str:
    """The summary table, then every question per configuration (score, cost, time, missed)."""
    per_question = [
        {"question": r["id"], "config": r["config"], "score": f"{r['score']:.2f}",
         "cited": r["cited"], "$": f"{r['cost_usd']:.3f}", "s": r["seconds"],
         "missed": ", ".join(r["missed"])}
        for r in sorted(results, key=lambda r: (r["id"], r["config"])) if "score" in r
    ]  # fmt: skip
    return "\n".join(table(summary(results)) + [""] + table(per_question)) + "\n"
