"""The specialists (PLAN.md §7): each one is a tool loop with its own prompt, tools and model,
behind `run(AgentTask) -> AgentResult`. Findings may cite only evidence the agent's own tool
calls returned in this task; anything else is dropped as invented."""

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from knesset.agents.contracts import AgentResult, AgentTask, Evidence, Finding
from knesset.agents.llm import Budget, LimitReached, tool_loop
from knesset.config import settings
from knesset.tools.lc import AGENT_TOOLS
from knesset.tracing import tracer

PROMPTS = Path(__file__).with_name("prompts")
MAX_FINDINGS = 6  # per task, most important first: shorter drafts, fewer claims to check
EFFORT = {"analyst": "medium", "researcher": "medium", "summarizer": "medium", "verifier": "medium"}


class Answer(BaseModel):
    findings: list[Finding] = Field(description="Every claim with its evidence")
    gaps: str = Field(description="What could not be found or checked; empty if nothing")


class Issue(BaseModel):
    claim: str = Field(description="The draft's claim, briefly")
    problem: str = Field(description="What is wrong, and what the source shows instead")
    kind: Literal["unsupported", "missing"] = Field(
        description="unsupported: the claim is wrong, overstated or not in its source; "
        "missing: the answer needs information the findings do not have"
    )


class Verdict(BaseModel):
    status: Literal["pass", "fail"]
    issues: list[Issue] = Field(description="Empty on pass")


def prompt(name: str) -> str:
    return (
        (PROMPTS / "common.md").read_text(encoding="utf-8")
        + "\n\n"
        + (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    )


def model_for(agent: str) -> str:
    return getattr(settings(), f"agent_{agent}_model")


def tools(agent: str) -> list:
    return list(AGENT_TOOLS[agent])


def seen_refs(artifacts: list[dict]) -> set[tuple[str, str]]:
    """(type, ref) of every piece of evidence the tools returned."""
    seen = set()
    for a in artifacts:
        out = a["output"] if isinstance(a["output"], dict) else {}
        for e in out.get("evidence", []):
            seen.add((e["type"], norm(e["type"], str(e["ref"]))))
    return seen


def norm(kind: str, ref: str) -> str:
    return re.sub(r"\s+", " ", ref).strip().lower() if kind == "sql" else ref.strip()


def keep_seen(findings: list[Finding], seen: set[tuple[str, str]]) -> list[Finding]:
    """Evidence the tools returned; a claim left without any becomes low confidence."""
    out = []
    for f in findings:
        ev = [e for e in f.evidence if (e.type, norm(e.type, e.ref)) in seen]
        out.append(
            f.model_copy(update={"evidence": ev, "confidence": f.confidence if ev else "low"})
        )
    return out


def run(task: AgentTask, budget: Budget, api: Any = None) -> AgentResult:
    """One specialist task. Errors and limits come back in the result, not as exceptions."""
    agent = task.agent
    with tracer().start_as_current_span(f"agent.{agent}") as span:
        span.set_attribute("agent", agent)
        span.set_attribute("task_id", task.task_id)
        span.set_attribute("instruction", task.instruction[:500])
        text = f"Task: {task.instruction}"
        if task.filters:
            text += f"\n\nFilters from the user (always apply): {task.filters}"
        if task.context:
            text += f"\n\nContext:\n{task.context}"
        output = Verdict if agent == "verifier" else Answer
        try:
            budget.step(f"{agent} {task.task_id}")
            answer, usage, artifacts = tool_loop(
                model_for(agent), prompt(agent), text, output, budget,
                tools(agent), EFFORT[agent], api,
            )  # fmt: skip
        except (LimitReached, RuntimeError, ValueError) as e:
            span.set_attribute("error", str(e))
            return AgentResult(task_id=task.task_id, agent=agent, error=str(e))
        span.set_attribute("cost_usd", usage.cost_usd)
        result = AgentResult(task_id=task.task_id, agent=agent, usage=usage, artifacts=artifacts)
        if isinstance(answer, Verdict):
            result.verdict = answer.model_dump()
        else:
            result.findings = keep_seen(answer.findings, seen_refs(artifacts))[:MAX_FINDINGS]
            if answer.gaps:
                result.findings.append(
                    Finding(claim=f"Gaps: {answer.gaps}", evidence=[], confidence="low")
                )
        return result


__all__ = ["Answer", "Evidence", "Issue", "Verdict", "run"]
