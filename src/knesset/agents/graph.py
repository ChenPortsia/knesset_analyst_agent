"""The orchestrator graph (PLAN.md §7) on LangGraph:

- State with reducers; the runtime context (`context_schema`) carries the question's budget
  and the model factory, so tests pass fake models and other providers plug in.
- start → plan: a `create_agent` subgraph (resolve_entity, native structured Plan). Names it
  resolves go to the long-term Store and are offered to later plans in any thread.
- clarify: `interrupt()` pauses the run with a question; `Command(resume=…)` continues it.
- dispatch → specialist: `Send` fan-out, independent tasks in parallel, dependent ones in a
  later wave with their dependencies' findings as context.
- draft / revise: structured output, with a `RetryPolicy` on transient errors.
- verify: SQL-only answers checked in code; otherwise the verifier subgraph. It routes with
  `Command(goto=…)`: finish, revise (unsupported claims), or plan once more (missing
  information, if budget is left).
- finish: the answer; the checkpointer keeps the thread's conversation for follow-ups.
- Streaming: graph and subgraph updates (tool calls) and custom events (agent start/finish).
"""

import json
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from operator import add
from typing import Annotated, Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, RetryPolicy, Send, interrupt
from pydantic import BaseModel, Field

from knesset.agents.client import client_for
from knesset.agents.contracts import AgentResult, AgentTask, Finding
from knesset.agents.llm import Budget, LimitReached
from knesset.agents.models import BudgetCallback, ModelFactory, chat_model, effort
from knesset.agents.specialist import last_call, middleware, prompt, run_agent
from knesset.config import settings
from knesset.tools.common import resolve_entity
from knesset.tools.lc import as_tool
from knesset.tools.sql import sql_query
from knesset.tracing import tracer

REDELEGATE_RESERVE_USD = 0.15  # re-plan for missing information only if this much is left
RETRY = RetryPolicy(max_attempts=2)  # transient API / network errors (not limits or bad output)
MEMORY_SCHEMA = "ops"  # checkpoints and the long-term store: tables in etl's `ops` schema


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


def extend(left: list | None, right: list | None) -> list:
    """Append results; None resets (a new question in the same thread)."""
    return [] if right is None else (left or []) + right


class State(TypedDict, total=False):
    question: str
    user_id: str
    ui_filters: dict[str, Any]
    conversation: Annotated[list[dict], add]  # earlier turns in this thread (checkpointed)
    plan: list[dict]
    round: int
    clarification: str
    results: Annotated[list[dict], extend]  # AgentResult per task
    draft: dict
    sources: list[dict]
    verdict: dict
    redelegated: bool  # the one re-plan for missing information was used
    revised: bool
    limit: str  # why the run stopped early
    answer: str


class TaskState(TypedDict):
    """What `Send` hands one specialist."""

    task: dict
    context: str
    filters: dict[str, Any]


@dataclass
class Context:
    """Runtime context of one run: not part of the checkpointed state."""

    budget: Budget = field(default_factory=Budget.from_settings)
    models: ModelFactory = chat_model

    def config(self, step: str) -> dict:
        """Model-call config for one graph step: its cost is charged to that step."""
        return {"callbacks": [BudgetCallback(self.budget, step)]}


# ---------------------------------------------------------------- formatting


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


def tag(name: str, body: str) -> str:
    """Data for a prompt, inside XML tags, apart from the instructions."""
    return f"<{name}>\n{body}\n</{name}>"


def evidence(results: list[dict], sources: list[dict]) -> str:
    """The findings (sources in brackets) and the numbered sources, tagged."""
    return (tag("findings", findings_text(results, sources)) + "\n\n"
            + tag("sources", sources_text(sources)))  # fmt: skip


def sources_text(sources: list[dict]) -> str:
    return "\n".join(f"[{s['n']}] {s['type']} {s['ref']} {s.get('url') or ''}" for s in sources)


NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")


def numbers(text: str) -> set[str]:
    """Numbers in a text, normalized: no thousands commas, no citation markers, dates split."""
    text = DATE.sub(r"\1 \2 \3", re.sub(r"\[\d+\]", " ", text))
    out = set()
    for n in NUMBER.findall(text):
        n = n.replace(",", "")
        out.add(str(float(n)).removesuffix(".0") if "." in n else str(int(n)))
    return out


def source_numbers(sources: list[dict], passages: dict[str, str]) -> set[str] | None:
    """Every number the sources hold: SQL re-run now, `compute` results, cited passages.
    None when a query fails to re-run (then the verifier judges)."""
    found: set[str] = set()
    for s in sources:
        if s["type"] == "calc":
            found |= numbers(s["ref"])
        elif s["type"] == "chunk":
            found |= numbers(passages.get(str(s["ref"]), ""))
        elif s["type"] == "sql":
            try:
                rows = sql_query(s["ref"])["rows"]
            except Exception:  # noqa: BLE001 — the verifier's to judge
                return None
            for cell in (c for row in rows for c in row):
                found |= numbers(str(cell))
                if isinstance(cell, float):
                    found |= {f"{cell:.1f}".removesuffix(".0"), str(round(cell))}
    return found


def numbers_check(
    question: str, answer: str, sources: list[dict], passages: dict[str, str]
) -> bool:
    """Every number in the answer appears in its sources (or the question, or is a rank ≤ 20)."""
    found = source_numbers(sources, passages)
    if found is None:
        return False
    claimed = numbers(answer)
    return bool(claimed) and claimed <= found | numbers(question) | {str(i) for i in range(21)}


NUMBERS_CHECKED = (
    "\n\nEvery number in the draft appears in its sources (checked in code): check names, "
    "dates, quotes and attributions; do not re-run the SQL."
)


def passages_of(results: list[dict]) -> dict[str, str]:
    """chunk id → speaker, date and text of every chunk the specialists' tools returned."""
    out: dict[str, str] = {}
    for r in results:
        for a in r.get("artifacts", []):
            output = a.get("output") if isinstance(a.get("output"), dict) else {}
            for doc in output.get("results", []):
                for c in doc.get("chunks", []):
                    head = f"{c.get('speaker') or ''} ({doc.get('date') or ''})".strip()
                    out[str(c["chunk_id"])] = f"{head}: {c['text']}"
    return out


# ---------------------------------------------------------------- memory (Store)


def remember(store: Any, user_id: str, messages: list) -> None:
    """Keep each name the planner resolved, so later questions start from the ids."""
    if store is None:
        return
    calls = {c["id"]: c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
    for m in messages:
        if isinstance(m, ToolMessage) and m.status != "error":
            call = calls.get(m.tool_call_id, {})
            if call.get("name") != "resolve_entity":
                continue
            try:
                best = json.loads(m.content)["candidates"][0]
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            store.put(("entities", user_id), call["args"]["name"], best)


def known_entities(store: Any, user_id: str) -> str:
    if store is None:
        return ""
    items = store.search(("entities", user_id), limit=40)
    return "\n".join(f"- {i.key} → {i.value['type']} {i.value['id']} ({i.value['name']}, "
                     f"Knesset {i.value.get('knesset')})" for i in items)  # fmt: skip


# ---------------------------------------------------------------- nodes


def traced(name: str, step: Callable, guard: bool = True) -> Callable:
    """A node with its span; guarded nodes turn a hit limit or a failed model call (refusal,
    unparsable answer) into state, so the graph goes on to finish with what it has."""

    def node(state: State, runtime: Runtime[Context]) -> Any:
        if guard and state.get("limit"):
            return {}
        with tracer().start_as_current_span(f"orchestrator.{name}") as span:
            if not guard:
                return step(state, runtime)
            try:
                return step(state, runtime)
            except (LimitReached, RuntimeError, ValueError) as e:
                span.set_attribute("limit", str(e)[:300])
                return {"limit": str(e)[:300]}

    node.__name__ = name
    return node


def start(state: State, runtime: Runtime[Context]) -> State:
    """A new question: clear the previous question's working state (the conversation stays)."""
    return {"plan": [], "round": 0, "clarification": "", "results": None, "draft": {},
            "sources": [], "verdict": {}, "redelegated": False, "revised": False, "limit": "",
            "answer": ""}  # fmt: skip


def plan(state: State, runtime: Runtime[Context]) -> State:
    ctx = runtime.context
    ctx.budget.step("plan")
    user = state.get("user_id", "anonymous")
    parts = []  # context first, the question and the instruction last
    if known := known_entities(runtime.store, user):
        parts.append("Ids resolved in earlier questions (reuse them):\n" + tag("known_ids", known))
    if turns := state.get("conversation", [])[-3:]:
        earlier = "\n".join(f"Q: {t['question']}\nA: {t['answer'][:600]}" for t in turns)
        parts.append("Earlier in this conversation:\n" + tag("conversation", earlier))
    question = f"Question: {state['question']}"
    if state.get("ui_filters"):
        question += f"\nFilters set by the user: {json.dumps(state['ui_filters'])}"
    parts.append(question)
    if state.get("verdict", {}).get("status") == "fail":
        missing = [i["problem"] for i in state["verdict"]["issues"] if i["kind"] == "missing"]
        found = findings_text(state["results"], sources_of(state["results"]))
        parts.insert(-1, tag("findings_so_far", found))
        parts.append(
            "The verifier found information missing from a first answer:\n- "
            + "\n- ".join(missing)
            + "\nPlan only the tasks (at most 2) that find the missing information."
        )
    text = "\n\n".join(parts)
    planner = create_agent(
        model=ctx.models("orchestrator", effort("orchestrator")),
        tools=[as_tool(resolve_entity)],
        system_prompt=prompt("orchestrator"),
        response_format=ProviderStrategy(Plan),
        middleware=middleware(6),
        name="planner",
    )
    messages, result = run_agent(planner, text, ctx.config("plan"))
    if result is None:  # out of calls or a broken JSON: plan from what the tools returned
        model = ctx.models("orchestrator", effort("orchestrator"))
        last = last_call(model, Plan, prompt("orchestrator"), messages, ctx.config("plan"))
        result = last["parsed"]
    if result is None:
        raise RuntimeError("the planner gave no plan")
    remember(runtime.store, user, messages)
    rnd = state.get("round", 0) + 1
    tasks = [t.model_dump() for t in result.tasks]
    if rnd > 1:  # a re-plan: keep task ids unique across rounds
        for t in tasks:
            t["id"], t["depends_on"] = f"r{rnd}.{t['id']}", [f"r{rnd}.{d}" for d in t["depends_on"]]
    update: State = {"plan": tasks, "clarification": result.clarification, "round": rnd}
    return update | ({"redelegated": True} if state.get("verdict") else {})


def after_plan(state: State) -> str:
    if state.get("limit"):
        return "finish"
    if state.get("clarification") and not state.get("plan"):
        return "clarify"
    return "dispatch"


def clarify(state: State, runtime: Runtime[Context]) -> State:
    """Pause and ask the user; the run continues with their reply (Command(resume=…))."""
    reply = interrupt({"question": state["clarification"]})
    return {"question": f"{state['question']}\n\nThe user clarified: {reply}", "clarification": ""}


def fan_out(state: State) -> list[Send] | str:
    """Send each ready task to a specialist (in parallel); when none are left, draft."""
    if state.get("limit"):
        return "finish"
    done = {r["task_id"]: r for r in state.get("results") or []}
    pending = [t for t in state.get("plan", []) if t["id"] not in done]
    if not pending:
        return "draft"
    ids = {t["id"] for t in state["plan"]}
    ready = [t for t in pending if all(d in done or d not in ids for d in t["depends_on"])]
    sends = []
    for t in ready or pending:  # a dependency cycle: run the rest without their context
        deps = [done[d] for d in t["depends_on"] if d in done]
        context = findings_text(deps, sources_of(deps)) if deps else ""
        sends.append(Send("specialist", {"task": t, "context": context,
                                         "filters": state.get("ui_filters", {})}))  # fmt: skip
    return sends


def specialist(payload: TaskState, runtime: Runtime[Context]) -> State:
    """One delegated task, through AgentClient (in process now, A2A in Phase 8)."""
    ctx, t = runtime.context, payload["task"]
    runtime.stream_writer({"agent": t["agent"], "task": t["id"], "status": "started"})
    task = AgentTask(task_id=t["id"], agent=t["agent"], instruction=t["instruction"],
                     context=payload["context"], filters=payload["filters"],
                     max_cost_usd=ctx.budget.left_usd)  # fmt: skip
    with tracer().start_as_current_span(f"handoff.{t['agent']}") as span:
        span.set_attribute("task_id", t["id"])
        span.set_attribute("from", "orchestrator")
        span.set_attribute("to", t["agent"])
        result: AgentResult = client_for(t["agent"]).run(task, ctx.budget, ctx.models)
    runtime.stream_writer({"agent": t["agent"], "task": t["id"], "status": "done",
                           "findings": len(result.findings), "error": result.error,
                           "cost_usd": round(result.usage.cost_usd, 4)})  # fmt: skip
    return {"results": [result.model_dump()]}


def structured(ctx: Context, step: str, schema: type[BaseModel], shared: str, rest: str) -> Any:
    """One orchestrator call for a JSON answer. `shared` (the findings and sources) starts both
    the draft and its revision, so it is cached on the draft call and read cheaply by the
    revision; `rest` is the question and the instruction."""
    from langchain_anthropic import ChatAnthropic

    chat = ctx.models("orchestrator", effort("orchestrator"))
    first: dict[str, Any] = {"type": "text", "text": shared}
    if isinstance(chat, ChatAnthropic):
        first["cache_control"] = {"type": "ephemeral"}
    message = HumanMessage([first, {"type": "text", "text": rest}])
    model = chat.with_structured_output(schema, method="json_schema")
    return model.invoke([SystemMessage(prompt("draft")), message], ctx.config(step))


def draft(state: State, runtime: Runtime[Context]) -> State:
    ctx = runtime.context
    ctx.budget.step("draft")
    sources = sources_of(state["results"])
    rest = f"Question: {state['question']}\n\nWrite the answer from the findings above."
    result: Draft = structured(ctx, "draft", Draft, evidence(state["results"], sources), rest)
    return {"draft": result.model_dump(), "sources": sources}


def after_draft(state: State) -> str:
    return "finish" if state.get("limit") else "verify"


def verify(state: State, runtime: Runtime[Context]) -> Command[Literal["finish", "revise", "plan"]]:
    ctx, sources = runtime.context, state["sources"]
    question, answer = state["question"], state["draft"]["answer"]
    passages = passages_of(state["results"])
    numbers_ok = numbers_check(question, answer, sources, passages)
    if numbers_ok and all(s["type"] in ("sql", "calc") for s in sources):
        verdict = {"status": "pass", "issues": [], "checked": "sql in code"}
        return Command(goto="finish", update={"verdict": verdict})
    cited = [s for s in sources if s["type"] == "chunk" and str(s["ref"]) in passages]
    context = evidence(state["results"], sources)
    if cited:  # already read: the verifier needs get_chunks only for the others
        read = "\n".join(
            f"[{s['n']}] chunk {s['ref']} — {passages[str(s['ref'])][:1500]}" for s in cited[:10]
        )
        context += "\n\nCited passages (already read):\n" + tag("passages", read)
    context += f"\n\nQuestion: {question}\n\n{tag('draft', answer)}"
    if numbers_ok:
        context += NUMBERS_CHECKED
    task = AgentTask(
        task_id="verify",
        agent="verifier",
        instruction="Check this draft answer against its sources.",
        context=context,
        max_cost_usd=ctx.budget.left_usd,
    )
    with tracer().start_as_current_span("handoff.verifier"):
        result = client_for("verifier").run(task, ctx.budget, ctx.models)
    if result.verdict is None:  # the check itself failed: say so, do not loop
        problem = {"claim": "", "problem": result.error or "no verdict", "kind": "unsupported"}
        verdict = {"status": "fail", "issues": [problem]}
        if "limit" in (result.error or ""):
            return Command(goto="finish", update={"verdict": verdict, "limit": result.error})
        return Command(goto="revise", update={"verdict": verdict})
    verdict = result.verdict
    if verdict["status"] != "fail":
        return Command(goto="finish", update={"verdict": verdict})
    missing = any(i["kind"] == "missing" for i in verdict["issues"])
    if missing and not state.get("redelegated") and ctx.budget.left_usd >= REDELEGATE_RESERVE_USD:
        return Command(goto="plan", update={"verdict": verdict})
    return Command(goto="revise", update={"verdict": verdict})


def revise(state: State, runtime: Runtime[Context]) -> State:
    """Think again without searching: rewrite from the findings, fixing each issue."""
    ctx = runtime.context
    ctx.budget.step("revise")
    sources = state["sources"]
    issues = "\n".join(f"- {i['claim']}: {i['problem']}" for i in state["verdict"]["issues"])
    draft = tag("draft", state["draft"]["answer"])
    rest = (
        f"Question: {state['question']}\n\nYour draft:\n{draft}"
        f"\n\nThe verifier found these issues:\n{tag('issues', issues)}\n\n"
        "Rewrite the answer: remove or correct each flagged claim, or state it as uncertain. "
        "Add no new fact. Write it as clean final text: no wrong values left in, no editing "
        "notes. Say in caveats what you removed."
    )
    result: Draft = structured(ctx, "revise", Draft, evidence(state["results"], sources), rest)
    return {"draft": result.model_dump(), "revised": True}


def finish(state: State, runtime: Runtime[Context]) -> State:
    sources = state.get("sources") or sources_of(state.get("results") or [])
    if state.get("draft"):
        answer = state["draft"]["answer"]
        notes = [state["draft"]["caveats"]] if state["draft"]["caveats"] else []
    else:  # a limit stopped the run before the draft: list the findings
        answer = "Partial findings:\n" + findings_text(state.get("results") or [], sources)
        notes = []
    if state.get("verdict", {}).get("status") == "fail" and not state.get("revised"):
        issues = "; ".join(i["problem"] for i in state["verdict"]["issues"])
        notes.append(f"Not fully verified: {issues}")
    if state.get("limit"):
        notes.append(f"Stopped early: {state['limit']}.")
    if notes:
        answer += "\n\n" + "\n".join(f"⚠ {n}" for n in notes)
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    sources = [s for s in sources if s["n"] in cited]  # only what the answer cites
    turn = {"question": state["question"], "answer": answer}
    return {"answer": answer, "sources": sources, "conversation": [turn]}


# ---------------------------------------------------------------- graph


def build(checkpointer: Any = None, store: Any = None) -> Any:
    g = StateGraph(State, context_schema=Context)
    g.add_node("start", traced("start", start, guard=False))
    g.add_node("plan", traced("plan", plan), retry_policy=RETRY)
    g.add_node("clarify", traced("clarify", clarify, guard=False))
    g.add_node("dispatch", lambda state: {})
    g.add_node("specialist", specialist)
    g.add_node("draft", traced("draft", draft), retry_policy=RETRY)
    g.add_node(
        "verify", traced("verify", verify, guard=False), destinations=("finish", "revise", "plan")
    )
    g.add_node("revise", traced("revise", revise), retry_policy=RETRY)
    g.add_node("finish", traced("finish", finish, guard=False))
    g.add_edge(START, "start")
    g.add_edge("start", "plan")
    g.add_conditional_edges("plan", after_plan, ["finish", "clarify", "dispatch"])
    g.add_edge("clarify", "plan")
    g.add_conditional_edges("dispatch", fan_out, ["specialist", "draft", "finish"])
    g.add_edge("specialist", "dispatch")
    g.add_conditional_edges("draft", after_draft, ["finish", "verify"])
    g.add_edge("revise", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer, store=store)


studio_graph = build()  # LangGraph Studio (`langgraph dev`) supplies its own persistence


def memory_dsn() -> str:
    return f"{settings().dsn('etl')}?options=-csearch_path%3D{MEMORY_SCHEMA}"


@contextmanager
def persistence(postgres: bool = True) -> Iterator[tuple[Any, Any]]:
    """Checkpointer and Store: in Postgres (tables in schema `ops`), or in memory."""
    if not postgres:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore

        yield InMemorySaver(), InMemoryStore()
        return
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore

    with (
        PostgresSaver.from_conn_string(memory_dsn()) as saver,
        PostgresStore.from_conn_string(memory_dsn()) as store,
    ):
        saver.setup()
        store.setup()
        yield saver, store


def ask(
    question: str = "",
    user_id: str = "cli",
    thread_id: str | None = None,
    filters: dict[str, Any] | None = None,
    models: ModelFactory | None = None,
    graph: Any = None,
    resume: str | None = None,
) -> Iterator[tuple[str, dict]]:
    """Run one question (or resume a paused one) and stream it: ("<node>", update),
    ("event", custom progress), ("tool", a subgraph's tool call), ("interrupt", question), then
    ("done", result). Pass a `graph` built with a checkpointer to keep the conversation."""
    graph = graph or build(*_memory())
    thread_id = thread_id or uuid.uuid4().hex
    ctx = Context(budget=Budget.from_settings(), models=models or chat_model)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}
    inp: Any = (
        Command(resume=resume)
        if resume is not None
        else {"question": question, "user_id": user_id, "ui_filters": filters or {}}
    )
    started = time.monotonic()
    paused = None
    with tracer().start_as_current_span("question") as span:
        span.set_attribute("user_id", user_id)
        span.set_attribute("thread_id", thread_id)
        span.set_attribute("question", question[:500])
        span.set_attribute("langfuse.user.id", user_id)
        span.set_attribute("langfuse.session.id", thread_id)
        span.set_attribute("input.value", question or f"(resume) {resume}")
        path = []
        for ns, mode, data in graph.stream(inp, config, context=ctx, subgraphs=True,
                                           stream_mode=["updates", "custom"]):  # fmt: skip
            if mode == "custom":
                yield "event", data
            elif ns:  # inside a subgraph: report its tool calls
                for change in data.values():
                    for m in (change or {}).get("messages", []):
                        for call in getattr(m, "tool_calls", None) or []:
                            yield "tool", {"name": call["name"], "args": call["args"]}
            else:
                for node, change in data.items():
                    if node == "__interrupt__":
                        paused = change[0].value
                        yield "interrupt", paused | {"thread_id": thread_id}
                        continue
                    path.append(node)
                    yield node, change or {}
        final = graph.get_state(config).values
        span.set_attribute("path", " → ".join(path))
        span.set_attribute("answer", final.get("answer", "")[:2000])
        span.set_attribute("output.value", final.get("answer", ""))
        span.set_attribute("verdict", (final.get("verdict") or {}).get("status", ""))
        span.set_attribute("cost_usd", ctx.budget.usage.cost_usd)
        span.set_attribute("steps", ctx.budget.steps)
        trace_id = format(span.get_span_context().trace_id, "032x")
    yield "done", {
        "answer": "" if paused else final.get("answer", ""),
        "interrupt": paused,
        "thread_id": thread_id,
        "sources": final.get("sources", []),
        "verdict": final.get("verdict"),
        "results": final.get("results", []),
        "revised": bool(final.get("revised")),
        "cost_by_step": {k: round(v, 5) for k, v in ctx.budget.by_step.items()},
        "usage": ctx.budget.usage.model_dump() | {"steps": ctx.budget.steps,
                                                  "seconds": round(time.monotonic() - started, 1)},
        "trace_id": trace_id,
    }  # fmt: skip


_default_memory: tuple[Any, Any] | None = None


def _memory() -> tuple[Any, Any]:
    """In-memory checkpointer and store for calls that pass no graph (one process)."""
    global _default_memory
    if _default_memory is None:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore

        _default_memory = (InMemorySaver(), InMemoryStore())
    return _default_memory


__all__ = ["AgentResult", "Context", "Finding", "ask", "build", "persistence"]
