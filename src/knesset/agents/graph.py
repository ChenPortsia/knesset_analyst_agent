"""The orchestrator graph (PLAN.md §7): plan → delegate → draft → verify → finish.

- plan: the orchestrator (Opus) writes tasks for the specialists, or asks the user to clarify;
  it may call resolve_entity so filters use ids.
- delegate: tasks run through AgentClient, independent ones in parallel, dependent ones after
  the tasks they need (with those findings as context).
- draft: an answer from the findings only, citing numbered sources.
- verify: the verifier checks the draft against the cited chunks and SQL. On fail the
  orchestrator plans again once with the issues; after that, the answer carries caveats.
- Limits (cost, steps, 90 s) end the run early with what is known, said so.
"""

import json
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from knesset.agents.client import client_for
from knesset.agents.contracts import AgentResult, AgentTask, Finding
from knesset.agents.llm import Budget, LimitReached, tool_loop
from knesset.agents.specialist import model_for, prompt
from knesset.config import settings
from knesset.tools.common import resolve_entity
from knesset.tracing import tracer

MAX_REPLANS = 1


class Task(BaseModel):
    id: str = Field(description="t1, t2, …")
    agent: Literal["analyst", "researcher", "summarizer"]
    instruction: str = Field(description="What to find, with the ids and filters to use")
    depends_on: list[str] = Field(description="Ids of tasks whose findings this one needs")


class Plan(BaseModel):
    clarification: str = Field(description="A question for the user if the request is unclear")
    tasks: list[Task]


class Draft(BaseModel):
    answer: str = Field(description="The answer in the question's language, citing [n]")
    caveats: str = Field(description="What is uncertain or missing; empty if nothing")


class State(TypedDict, total=False):
    question: str
    user_id: str
    thread_id: str
    ui_filters: dict[str, Any]
    plan: list[dict]
    clarification: str
    results: list[dict]  # AgentResult per task
    draft: dict
    verdict: dict
    replans: int
    limit: str  # why the run stopped early
    answer: str
    sources: list[dict]


def sources_of(results: list[dict]) -> list[dict]:
    """Every distinct piece of evidence in the findings, numbered from 1."""
    out: dict[tuple[str, str], dict] = {}
    for r in results:
        for f in r.get("findings", []):
            for e in f["evidence"]:
                out.setdefault((e["type"], e["ref"]), {"n": len(out) + 1, **e})
    return list(out.values())


def findings_text(results: list[dict], sources: list[dict]) -> str:
    """Findings with their source numbers, for the draft, the verifier and dependent tasks."""
    number = {(s["type"], s["ref"]): s["n"] for s in sources}
    lines = []
    for r in results:
        if r.get("error"):
            lines.append(f"- ({r['agent']} {r['task_id']} failed: {r['error']})")
        for f in r.get("findings", []):
            refs = "".join(f"[{number[(e['type'], e['ref'])]}]" for e in f["evidence"])
            lines.append(f"- {f['claim']} {refs} (confidence: {f['confidence']})")
    return "\n".join(lines) or "(no findings)"


def sources_text(sources: list[dict]) -> str:
    return "\n".join(f"[{s['n']}] {s['type']} {s['ref']} {s.get('url') or ''}" for s in sources)


class Orchestrator:
    def __init__(self, budget: Budget, api: Any = None):
        self.budget, self.api = budget, api

    def plan(self, state: State) -> State:
        self.budget.step("plan")
        text = f"Question: {state['question']}"
        if state.get("ui_filters"):
            text += f"\nFilters set by the user: {json.dumps(state['ui_filters'])}"
        if state.get("verdict", {}).get("status") == "fail":
            text += (
                "\n\nA first attempt was rejected by the verifier. Issues:\n- "
                + "\n- ".join(state["verdict"]["issues"])
                + "\n\nFindings so far:\n"
                + findings_text(state["results"], sources_of(state["results"]))
                + "\n\nPlan only the tasks that fix these issues."
            )
        plan, _, _ = tool_loop(model_for("orchestrator"), prompt("orchestrator"), text, Plan,
                               self.budget, [resolve_entity], "medium", self.api)  # fmt: skip
        return {"plan": [t.model_dump() for t in plan.tasks], "clarification": plan.clarification}

    def delegate(self, state: State) -> State:
        tasks = {t["id"]: t for t in state["plan"]}
        done: dict[str, dict] = {}
        earlier = state.get("results", [])
        while len(done) < len(tasks):
            ready = [t for t in tasks.values() if t["id"] not in done
                     and all(d in done or d not in tasks for d in t["depends_on"])]  # fmt: skip
            if not ready:  # a dependency cycle: run the rest without their context
                ready = [t for t in tasks.values() if t["id"] not in done]

            def handoff(t: dict) -> dict:
                deps = [done[d] for d in t["depends_on"] if d in done]
                context = findings_text(deps, sources_of(deps)) if deps else ""
                task = AgentTask(task_id=t["id"], agent=t["agent"], instruction=t["instruction"],
                                 context=context, filters=state.get("ui_filters", {}),
                                 max_cost_usd=self.budget.left_usd)  # fmt: skip
                with tracer().start_as_current_span(f"handoff.{t['agent']}") as span:
                    span.set_attribute("task_id", t["id"])
                    span.set_attribute("from", "orchestrator")
                    span.set_attribute("to", t["agent"])
                    return client_for(t["agent"], self.api).run(task, self.budget).model_dump()

            with ThreadPoolExecutor(max_workers=4) as pool:
                for t, result in zip(ready, pool.map(handoff, ready), strict=True):
                    done[t["id"]] = result
        return {"results": earlier + list(done.values())}

    def draft(self, state: State) -> State:
        self.budget.step("draft")
        sources = sources_of(state["results"])
        text = (
            f"Question: {state['question']}\n\nFindings (sources in brackets):\n"
            f"{findings_text(state['results'], sources)}\n\nSources:\n{sources_text(sources)}"
        )
        draft, _, _ = tool_loop(model_for("orchestrator"), prompt("draft"), text, Draft,
                                self.budget, [], "medium", self.api)  # fmt: skip
        return {"draft": draft.model_dump(), "sources": sources}

    def verify(self, state: State) -> State:
        sources = state["sources"]
        task = AgentTask(
            task_id="verify",
            agent="verifier",
            instruction="Check this draft answer against its sources.",
            context=f"Question: {state['question']}\n\nDraft:\n{state['draft']['answer']}\n\n"
            f"Findings:\n{findings_text(state['results'], sources)}\n\n"
            f"Sources:\n{sources_text(sources)}",
            max_cost_usd=self.budget.left_usd,
        )
        with tracer().start_as_current_span("handoff.verifier"):
            result = client_for("verifier", self.api).run(task, self.budget)
        verdict = result.verdict or {"status": "fail", "issues": [result.error or "no verdict"]}
        if result.error and "limit" in result.error:
            return {"verdict": verdict, "limit": result.error}
        return {"verdict": verdict, "replans": state.get("replans", 0)
                + (verdict["status"] == "fail")}  # fmt: skip

    def finish(self, state: State) -> State:
        if state.get("clarification") and not state.get("plan"):
            return {"answer": state["clarification"], "sources": []}
        sources = state.get("sources") or sources_of(state.get("results", []))
        if state.get("draft"):
            answer = state["draft"]["answer"]
            notes = [state["draft"]["caveats"]] if state["draft"]["caveats"] else []
        else:  # a limit stopped the run before the draft: list the findings
            answer = "Partial findings:\n" + findings_text(state.get("results", []), sources)
            notes = []
        if state.get("verdict", {}).get("status") == "fail":
            notes.append("Not fully verified: " + "; ".join(state["verdict"]["issues"]))
        if state.get("limit"):
            notes.append(f"Stopped early: {state['limit']}.")
        if notes:
            answer += "\n\n" + "\n".join(f"⚠ {n}" for n in notes)
        return {"answer": answer, "sources": sources}


def guarded(step: Any) -> Any:
    """A node that turns a hit limit, or a failed model call (refusal, unparsable answer), into
    state, so the graph goes on to finish with what it has."""

    def node(state: State) -> State:
        if state.get("limit"):
            return {}
        try:
            return step(state)
        except (LimitReached, RuntimeError, ValueError) as e:
            return {"limit": str(e)[:300]}

    node.__name__ = step.__name__
    return node


def build(budget: Budget, api: Any = None) -> Any:
    o = Orchestrator(budget, api)
    g = StateGraph(State)
    for name in ("plan", "delegate", "draft", "verify", "finish"):
        g.add_node(name, guarded(getattr(o, name)) if name != "finish" else o.finish)
    g.set_entry_point("plan")
    g.add_conditional_edges(
        "plan", lambda s: "finish" if s.get("limit") or not s.get("plan") else "delegate"
    )
    g.add_conditional_edges("delegate", lambda s: "finish" if s.get("limit") else "draft")
    g.add_conditional_edges("draft", lambda s: "finish" if s.get("limit") else "verify")

    def after_verify(s: State) -> str:
        failed = s.get("verdict", {}).get("status") == "fail"
        return (
            "plan"
            if failed and not s.get("limit") and s.get("replans", 0) <= MAX_REPLANS
            else "finish"
        )

    g.add_conditional_edges("verify", after_verify)
    g.add_edge("finish", END)
    return g.compile()


def ask(
    question: str,
    user_id: str = "cli",
    thread_id: str | None = None,
    filters: dict[str, Any] | None = None,
    api: Any = None,
) -> Iterator[tuple[str, dict]]:
    """Run one question; yields (node, update) as the graph moves, then ("done", result)."""
    s = settings()
    budget = Budget(s.agent_max_cost_usd, s.agent_max_steps, s.agent_timeout_s)
    state: State = {
        "question": question,
        "user_id": user_id,
        "thread_id": thread_id or uuid.uuid4().hex,
        "ui_filters": filters or {},
    }
    started = time.monotonic()
    with tracer().start_as_current_span("question") as span:
        span.set_attribute("user_id", user_id)
        span.set_attribute("thread_id", state["thread_id"])
        span.set_attribute("question", question[:500])
        final: State = dict(state)
        for update in build(budget, api).stream(state, stream_mode="updates"):
            for node, change in update.items():
                final.update(change or {})
                yield node, change or {}
        span.set_attribute("cost_usd", budget.usage.cost_usd)
        trace_id = format(span.get_span_context().trace_id, "032x")
    yield "done", {
        "answer": final.get("answer", ""),
        "sources": final.get("sources", []),
        "verdict": final.get("verdict"),
        "results": final.get("results", []),
        "usage": budget.usage.model_dump() | {"steps": budget.steps,
                                              "seconds": round(time.monotonic() - started, 1)},
        "trace_id": trace_id,
    }  # fmt: skip


__all__ = ["AgentResult", "Finding", "ask", "build"]
