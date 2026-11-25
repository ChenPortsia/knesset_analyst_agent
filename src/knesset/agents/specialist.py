"""The specialists (PLAN.md §7) as LangGraph subgraphs built with `create_agent`: each has its
own prompt, tools and model, a native structured final answer (findings, or the verifier's
verdict) and a cap on model calls, behind `run(AgentTask) -> AgentResult`. Findings may cite
only evidence the agent's own tool calls returned in this task; anything else is dropped."""

import json
import re
from pathlib import Path
from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from knesset.agents.contracts import AgentResult, AgentTask, Evidence, Finding, Usage
from knesset.agents.llm import Budget, LimitReached
from knesset.agents.models import BudgetCallback, ModelFactory, chat_model, model_name, usage_of
from knesset.tools.lc import tools_for
from knesset.tracing import tracer

PROMPTS = Path(__file__).with_name("prompts")
MAX_FINDINGS = 6  # per task, most important first: shorter drafts, fewer claims to check
EFFORT = {"analyst": "medium", "researcher": "medium", "summarizer": "medium", "verifier": "medium"}
MAX_CALLS = 8  # model calls per specialist task (middleware)


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


def subgraph(agent: str, model: Any) -> Any:
    """The specialist as a compiled LangGraph agent: model ↔ tools, then a native structured
    answer (Claude's JSON output, sent with the tools; forced tool choice is not used)."""
    return create_agent(
        model=model,
        tools=tools_for(agent),
        system_prompt=prompt(agent),
        response_format=ProviderStrategy(Verdict if agent == "verifier" else Answer),
        middleware=[ModelCallLimitMiddleware(run_limit=MAX_CALLS, exit_behavior="end")],
        name=agent,
    )


def last_call(model: Any, schema: type[BaseModel], system: str, messages: list, config: Any):
    """The tool loop ran out of model calls: one more call, without tools, from what the
    tools returned, for the structured answer. Returns {"raw": message, "parsed": answer}."""
    seen = "\n\n".join(f"{m.name}: {str(m.content)[:8000]}" for m in messages
                       if isinstance(m, ToolMessage))  # fmt: skip
    text = (f"{messages[0].content}\n\nTool results so far:\n{seen or '(none)'}\n\n"
            "No more tool calls: give your final answer now from these results.")  # fmt: skip
    model = model.with_structured_output(schema, method="json_schema", include_raw=True)
    return model.invoke([SystemMessage(system), HumanMessage(text)], config)


def artifacts_of(messages: list) -> list[dict]:
    """Each tool call in the subgraph's messages, with its input and its (JSON) result."""
    calls = {c["id"]: c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
    out = []
    for m in messages:
        if isinstance(m, ToolMessage):
            call = calls.get(m.tool_call_id, {})
            failed = m.status == "error"
            try:
                result = m.content if failed else json.loads(m.content)
            except (TypeError, ValueError):
                result, failed = m.content, True
            out.append({"tool": call.get("name", m.name), "input": call.get("args", {}),
                        "error": failed, "output": result})  # fmt: skip
    return out


def seen_refs(artifacts: list[dict]) -> dict[tuple[str, str], str | None]:
    """(type, ref) → url of every piece of evidence the tools returned."""
    seen: dict[tuple[str, str], str | None] = {}
    for a in artifacts:
        out = a["output"] if isinstance(a["output"], dict) else {}
        for e in out.get("evidence", []):
            seen[(e["type"], norm(e["type"], str(e["ref"])))] = e.get("url")
    return seen


def norm(kind: str, ref: str) -> str:
    return re.sub(r"\s+", " ", ref).strip().lower() if kind == "sql" else ref.strip()


def keep_seen(findings: list[Finding], seen: dict[tuple[str, str], str | None]) -> list[Finding]:
    """Evidence the tools returned, with the tool's url (the model may mistype it); a claim
    left without any evidence becomes low confidence."""
    out = []
    for f in findings:
        ev = [e.model_copy(update={"url": seen[key]}) for e in f.evidence
              if (key := (e.type, norm(e.type, e.ref))) in seen]  # fmt: skip
        out.append(
            f.model_copy(update={"evidence": ev, "confidence": f.confidence if ev else "low"})
        )
    return out


def run(task: AgentTask, budget: Budget, models: ModelFactory = chat_model) -> AgentResult:
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
        try:
            budget.step(f"{agent} {task.task_id}")
            model = models(agent, EFFORT[agent])
            config = {"callbacks": [BudgetCallback(budget)], "recursion_limit": 4 * MAX_CALLS}
            out = subgraph(agent, model).invoke({"messages": [HumanMessage(text)]}, config)
            messages = out["messages"]
            answer = out.get("structured_response")
            if answer is None:  # out of model calls
                schema = Verdict if agent == "verifier" else Answer
                last = last_call(model, schema, prompt(agent), messages, config)
                messages, answer = [*messages, last["raw"]], last["parsed"]
        except (LimitReached, RuntimeError, ValueError) as e:
            span.set_attribute("error", str(e))
            return AgentResult(task_id=task.task_id, agent=agent, error=str(e))
        usage = Usage()
        for m in messages:
            if isinstance(m, AIMessage):
                usage.add(usage_of(m, model_name(agent)))
        span.set_attribute("cost_usd", usage.cost_usd)
        artifacts = artifacts_of(messages)
        result = AgentResult(task_id=task.task_id, agent=agent, usage=usage, artifacts=artifacts)
        if answer is None:
            result.error = "no final answer"
        elif isinstance(answer, Verdict):
            result.verdict = answer.model_dump()
        else:
            result.findings = keep_seen(answer.findings, seen_refs(artifacts))[:MAX_FINDINGS]
            if answer.gaps:
                result.findings.append(
                    Finding(claim=f"Gaps: {answer.gaps}", evidence=[], confidence="low")
                )
        return result


__all__ = ["Answer", "Evidence", "Issue", "Verdict", "run"]
