"""The LangGraph agents end to end with a fake LangChain chat model (no paid calls)."""

import json
import re
import threading
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from test_search import chunks, encode  # noqa: F401
from test_tools import data  # noqa: F401  (search fixture + marts + sections)

from knesset.agents import graph
from knesset.agents.contracts import AgentTask, Evidence, Finding
from knesset.agents.llm import Budget
from knesset.agents.models import chat_model, usage_of
from knesset.agents.specialist import keep_seen, run
from knesset.config import settings

SQL = "select count(*) as people from marts.dim_person"
PLAN = {"clarification": "", "tasks": [
    {"id": "t1", "agent": "analyst", "instruction": "count people", "depends_on": []},
    {"id": "t2", "agent": "researcher", "instruction": "train delays", "depends_on": ["t1"]},
]}  # fmt: skip


def text_of(content: Any) -> str:
    """A message's text, also when it is a list of content blocks (cached prefixes)."""
    if isinstance(content, str):
        return content
    return "\n\n".join(b["text"] for b in content if b.get("type") == "text")


class Script:
    """Answers like each agent would. `verdicts` scripts the verifier ("pass", "unsupported" or
    "missing"); `cite` is the chunk id the researcher claims (None: the first it found);
    `answer` is the draft's text; `plan` the planner's plan."""

    def __init__(
        self, verdicts=("pass",), cite=None, plan=None, answer=None, stubborn=False, broken=False
    ):
        self.broken = broken  # the researcher's final JSON is cut off
        self.verdicts, self.cite, self.plan = list(verdicts), cite, plan or PLAN
        self.stubborn = stubborn  # the planner keeps calling tools until told to stop
        self.answer = answer or "There are 3 people [1]; the train is late [2]."
        self.prompts: list[tuple[str, str]] = []  # (agent, first human message)
        self.lock = threading.Lock()
        self.n = 0

    def models(self, agent: str, effort: str = "medium") -> BaseChatModel:
        return FakeChat(script=self, agent=agent)

    def call(self, tool: str, **args: Any) -> AIMessage:
        with self.lock:
            self.n += 1
            n = self.n
        return AIMessage("", tool_calls=[{"name": tool, "args": args, "id": f"call{n}"}])

    def final(self, obj: dict) -> AIMessage:
        return AIMessage(json.dumps(obj, ensure_ascii=False))

    def reply(self, agent: str, messages: list) -> AIMessage:
        system = messages[0].content if messages else ""
        human = next(text_of(m.content) for m in messages if m.type == "human")
        results = [m for m in messages if isinstance(m, ToolMessage)]
        planning = agent == "orchestrator" and "planning" in system
        with self.lock:
            self.prompts.append(("planner" if planning else agent, human))
        if planning:
            if "missing" in human:
                t3 = {"id": "t3", "agent": "researcher", "instruction": "more", "depends_on": []}
                return self.final({"clarification": "", "tasks": [t3]})
            if "No more tool calls" in human:
                return self.final(self.plan)
            if not results or self.stubborn:
                return self.call("resolve_entity", name="ועדת הכספים", types=["committee"])
            if self.plan.get("clarification") and "The user clarified" in human:
                return self.final(PLAN)
            return self.final(self.plan)
        if agent == "writer" and "verifier found these issues" in human:
            return self.final({"answer": "There are 3 people [1].", "caveats": "Removed the 4."})
        if agent == "writer":  # the draft
            return self.final({"answer": self.answer, "caveats": ""})
        if agent == "analyst":
            if not results:
                return self.call("sql_query", sql=SQL)
            evidence = [{"type": "sql", "ref": SQL, "url": None}]
            return self.final({"findings": [{"claim": "3 people", "confidence": "high",
                                             "evidence": evidence}], "gaps": ""})  # fmt: skip
        if agent == "researcher":
            if "No more tool calls" in human:  # the last call: cite the first evidence it saw
                ref = re.search(r"ref['\"]?:\s*(\d+)", human).group(1)
                evidence = [{"type": "chunk", "ref": ref, "url": None}]
                return self.final({"findings": [{"claim": "the train is late", "evidence": evidence,
                                                 "confidence": "high"}], "gaps": ""})  # fmt: skip
            if self.broken and results:
                return AIMessage('{"findings": [{"claim": "the train is la')
            if not results:
                return self.call("search_text", query="רכבת", filters={"committee_ids": [7]}, k=1)
            hit = json.loads(results[0].content)["evidence"][0]
            ref = str(self.cite or hit["ref"])
            evidence = [{"type": "chunk", "ref": ref, "url": "https://typed.by/the/model"}]
            return self.final({"findings": [{"claim": "the train is late", "confidence": "high",
                                             "evidence": evidence}], "gaps": ""})  # fmt: skip
        if agent == "verifier":
            if not results:
                return self.call("rerun_sql", sql=SQL)
            kind = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            if kind == "pass":
                return self.final({"status": "pass", "issues": []})
            issue = {"claim": "4 people", "problem": "the count is 3, not 4", "kind": kind}
            return self.final({"status": "fail", "issues": [issue]})
        raise AssertionError(f"unexpected agent {agent!r}")


class FakeChat(BaseChatModel):
    """A LangChain chat model that plays one agent from the Script."""

    script: Any
    agent: str

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeChat":
        return self

    def with_structured_output(self, schema: Any, include_raw: bool = False, **kw: Any) -> Any:
        def parse(msgs: Any, config: Any = None) -> Any:
            raw = self.invoke(msgs, config)
            parsed = schema.model_validate_json(raw.content)
            return {"raw": raw, "parsed": parsed} if include_raw else parsed

        return RunnableLambda(parse)

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kw: Any):
        msg = self.script.reply(self.agent, messages)
        msg.usage_metadata = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
        msg.response_metadata = {"model_name": "claude-sonnet-5-5"}
        return ChatResult(generations=[ChatGeneration(message=msg)])


def run_graph(script: Script, question: str = "כמה אנשים יש, ומה עם הרכבת?", **kw: Any):
    g = kw.pop("graph", None) or graph.build(*graph._memory())
    events = list(graph.ask(question, models=script.models, graph=g, **kw))
    nodes = [n for n, _ in events[:-1] if n not in ("event", "tool")]
    return nodes, events[-1][1], events


CYCLE = ["start", "plan", "dispatch", "specialist", "dispatch", "specialist", "dispatch", "draft"]


def test_chat_model_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    settings.cache_clear()
    orchestrator = chat_model("orchestrator", "low")
    assert orchestrator.model == "claude-sonnet-5-5"
    assert orchestrator.output_config == {"effort": "low"}
    assert orchestrator.model_kwargs == {"fallbacks": "default"}  # server-side refusal fallback
    verifier = chat_model("verifier")
    assert verifier.model == "claude-haiku-5-5" and not verifier.model_kwargs  # Haiku has none


def test_usage_prices_cache_reads():
    msg = AIMessage("x", usage_metadata={
        "input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
        "input_token_details": {"cache_read": 600, "cache_creation": 100}})  # fmt: skip
    msg.response_metadata = {"model_name": "claude-sonnet-5-5"}
    u = usage_of(msg)
    assert u.cost_usd == pytest.approx((300 * 2 + 600 * 0.10 + 100 * 2 * 1.25 + 100 * 10) / 1e6)


def test_keep_seen_drops_invented_evidence_and_fixes_urls():
    evidence = [
        Evidence(type="chunk", ref="7", url="https://typo"),
        Evidence(type="chunk", ref="9"),
    ]
    f = Finding(claim="x", confidence="high", evidence=evidence)
    (kept,) = keep_seen([f], {("chunk", "7"): "https://fs.knesset.gov.il/25/a.doc"})
    assert [e.ref for e in kept.evidence] == ["7"] and kept.confidence == "high"
    assert kept.evidence[0].url == "https://fs.knesset.gov.il/25/a.doc"
    (gone,) = keep_seen([f], {})
    assert gone.evidence == [] and gone.confidence == "low"


def test_specialist_subgraph_cites_only_what_it_saw(data):  # noqa: F811
    budget = Budget(1.0, 12, 90)
    task = AgentTask(task_id="t1", agent="researcher", instruction="train")
    seen = run(task, budget, Script().models)
    assert seen.findings[0].confidence == "high"
    assert seen.findings[0].evidence[0].url.startswith("https://fs.knesset.gov.il/")
    assert seen.artifacts[0]["tool"] == "search_text" and not seen.artifacts[0]["error"]
    invented = run(task, budget, Script(cite=123456789).models)
    assert invented.findings[0].evidence == [] and invented.findings[0].confidence == "low"
    assert budget.usage.calls == 4 and budget.steps == 2


def test_full_graph(data):  # noqa: F811
    script = Script()
    nodes, result, events = run_graph(script)
    assert nodes == CYCLE + ["verify", "finish"]
    assert result["answer"] == "There are 3 people [1]; the train is late [2]."
    assert [s["type"] for s in result["sources"]] == ["sql", "chunk"]
    assert result["verdict"] == {"status": "pass", "issues": []}
    # streamed: custom agent events and the subgraphs' tool calls
    custom = [e for n, e in events if n == "event"]
    assert {"agent": "analyst", "task": "t1", "status": "started"} in custom
    tools = {e["name"] for n, e in events if n == "tool"}
    assert tools >= {"resolve_entity", "sql_query", "search_text", "rerun_sql"}
    # the verifier gets the passages already read, and knows the numbers were checked in code
    (checked,) = {p for a, p in script.prompts if a == "verifier"}
    assert "Cited passages (already read)" in checked and "checked in code" in checked
    # the researcher depends on the analyst: it got the analyst's findings as context
    researcher = {p for a, p in script.prompts if a == "researcher"}
    assert len(researcher) == 1 and "3 people" in researcher.pop()
    u = result["usage"]
    assert u["steps"] == 5 and u["calls"] == 9  # plan 2, analyst 2, researcher 2, draft, verifier 2
    assert u["cost_usd"] == pytest.approx(9 * (1000 * 2 + 100 * 10) / 1e6)
    by_step = result["cost_by_step"]  # where the money went
    assert set(by_step) == {"plan", "analyst", "researcher", "draft", "verifier"}
    assert sum(by_step.values()) == pytest.approx(u["cost_usd"]) and not result["revised"]
    assert len(result["trace_id"]) == 32


def test_planner_out_of_calls_still_plans(data):  # noqa: F811
    """A planner that never stops calling tools gets one last call without them."""
    nodes, result, _ = run_graph(Script(stubborn=True))
    assert nodes == CYCLE + ["verify", "finish"] and result["answer"].startswith("There are 3")
    assert result["usage"]["calls"] == 9 - 2 + 6 + 1  # 6 planner calls, then the last one


def test_broken_final_json_gets_a_last_call(data):  # noqa: F811
    nodes, result, _ = run_graph(Script(broken=True))
    assert nodes == CYCLE + ["verify", "finish"]
    researcher = next(r for r in result["results"] if r["agent"] == "researcher")
    assert researcher["findings"][0]["confidence"] == "high" and not researcher["error"]


def test_sources_are_only_the_cited_ones(data):  # noqa: F811
    _, result, _ = run_graph(Script(answer="There are 3 people [1]."))
    assert [s["n"] for s in result["sources"]] == [1]


def test_unsupported_claims_are_revised_without_searching(data):  # noqa: F811
    script = Script(verdicts=["unsupported"])
    nodes, result, _ = run_graph(script)
    draft, revise = [p for a, p in script.prompts if a == "writer"]
    shared = draft.split("Question:")[0]  # findings + sources: the cached prefix of both
    assert "<findings>" in shared and revise.startswith(shared)
    assert result["revised"] and result["cost_by_step"]["revise"] > 0
    assert nodes == CYCLE + ["verify", "revise", "finish"]
    assert result["answer"].startswith("There are 3 people [1].")
    assert "Removed the 4." in result["answer"] and "Not fully verified" not in result["answer"]


def test_missing_information_is_delegated_once(data):  # noqa: F811
    nodes, result, _ = run_graph(Script(verdicts=["missing", "pass"]))
    assert nodes == CYCLE + ["verify", "plan", "dispatch", "specialist", "dispatch", "draft",
                             "verify", "finish"]  # fmt: skip
    assert [r["task_id"] for r in result["results"]] == ["t1", "t2", "r2.t3"]
    nodes, _, _ = run_graph(Script(verdicts=["missing", "missing"]))
    assert nodes[-3:] == ["verify", "revise", "finish"] and nodes.count("plan") == 2


def test_sql_only_answer_is_checked_in_code(data):  # noqa: F811
    task = {"id": "t1", "agent": "analyst", "instruction": "count people", "depends_on": []}
    plan = {"clarification": "", "tasks": [task]}
    script = Script(plan=plan, answer="There are 3 people [1].")
    _, result, _ = run_graph(script)
    assert result["verdict"] == {"status": "pass", "issues": [], "checked": "sql in code"}
    assert not any(a == "verifier" for a, _ in script.prompts)  # no model call for the check
    script = Script(plan=plan, answer="There are 40 people [1].", verdicts=["unsupported"])
    nodes, _, _ = run_graph(script)
    assert any(a == "verifier" for a, _ in script.prompts) and "revise" in nodes


def test_numbers_check_reads_sql_calc_and_passages():
    calc = {"type": "calc", "ref": "100 * 1 / 8 = 12.5"}
    chunk = {"type": "chunk", "ref": "7"}
    assert graph.numbers_check("q", "12.5% [1]", [calc], {})
    assert graph.numbers_check("q", "7.5 million tons [1]", [chunk], {"7": "about 7.5 million"})
    assert not graph.numbers_check("q", "90 million tons [1]", [chunk], {"7": "about 7.5"})


def test_numbers_normalize():
    assert graph.numbers("1,154 ישיבות [3], 91.5%, ב-24.3.2025") == {
        "1154", "91.5", "24", "3", "2025"
    }  # fmt: skip


def test_clarification_interrupts_and_resumes(data):  # noqa: F811
    """interrupt(): the run pauses with a question; Command(resume=…) continues it."""
    script = Script(plan={"clarification": "באיזו ועדה?", "tasks": []})
    g = graph.build(*graph._memory())
    nodes, paused, _ = run_graph(script, graph=g, thread_id="th1")
    assert nodes == ["start", "plan", "interrupt"]  # paused inside clarify
    assert paused["interrupt"]["question"] == "באיזו ועדה?"
    assert paused["answer"] == ""
    resumed = list(graph.ask(models=script.models, graph=g, thread_id="th1", resume="ועדת הכספים"))
    assert resumed[-1][1]["answer"].startswith("There are 3 people")
    asked = [p for a, p in script.prompts if a == "planner"]
    assert any("The user clarified: ועדת הכספים" in p for p in asked)


def test_conversation_and_memory_persist(data):  # noqa: F811
    """The checkpointer keeps the thread's conversation; the Store keeps resolved ids for the
    user, so a new thread starts from them too (Postgres, schema ops)."""
    with graph.persistence() as (saver, store):
        g = graph.build(saver, store)
        script = Script()
        run_graph(script, graph=g, thread_id="conv1", user_id="u1")
        run_graph(script, "ומה עם האוטובוסים?", graph=g, thread_id="conv1", user_id="u1")
        plans = [p for a, p in script.prompts if a == "planner"]
        follow_up = plans[-1]
        assert "Earlier in this conversation" in follow_up and "There are 3 people" in follow_up
        assert "Ids resolved in earlier questions" in follow_up and "ועדת הכספים" in follow_up
        run_graph(script, "שאלה חדשה", graph=g, thread_id="conv2", user_id="u1")
        fresh = [p for a, p in script.prompts if a == "planner"][-1]
        assert "Earlier in this conversation" not in fresh and "ועדת הכספים" in fresh
        assert store.get(("entities", "u1"), "ועדת הכספים").value["id"] == 7


def test_limits_end_early_with_what_is_known(data, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENT_MAX_COST_USD", "0.005")  # spent after two calls
    settings.cache_clear()
    nodes, result, _ = run_graph(Script())
    assert nodes[-1] == "finish" and "Stopped early: cost limit" in result["answer"]
    monkeypatch.setenv("AGENT_MAX_COST_USD", "1")
    monkeypatch.setenv("AGENT_MAX_STEPS", "1")
    settings.cache_clear()
    _, result, _ = run_graph(Script())
    assert "step limit" in result["answer"]


def cli(monkeypatch, script: Script, args: list[str], input: str | None = None):
    from typer.testing import CliRunner

    from knesset.cli import app

    monkeypatch.setattr(graph, "chat_model", script.models)
    monkeypatch.setattr(graph, "persistence", lambda postgres=True: _nullctx(graph._memory()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    settings.cache_clear()
    out = CliRunner().invoke(app, args, input=input)
    assert out.exit_code == 0, out.output
    return out.output


class _nullctx:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        return False


def test_ask_cli(data, monkeypatch):  # noqa: F811
    output = cli(monkeypatch, Script(), ["ask", "כמה אנשים יש?"])
    for line in ("plan   t1 → analyst", "plan   t2 → researcher (after t1)", "tool sql_query",
                 "agent  t1 analyst: 1 findings", "verify pass", "There are 3 people [1]",
                 "[1] sql select count(*)", "9 calls · 5 steps", "--thread "):  # fmt: skip
        assert line in output, output
    assert "tool_call" not in output  # tool-call log lines go to the log file


def test_ask_cli_asks_back(data, monkeypatch):  # noqa: F811
    script = Script(plan={"clarification": "באיזו ועדה?", "tasks": []})
    output = cli(monkeypatch, script, ["ask", "מה נאמר?"], input="ועדת הכספים\n")
    assert "באיזו ועדה?" in output and "There are 3 people" in output


def test_one_trace_per_question(data, monkeypatch):  # noqa: F811
    """Every span of a question, the parallel specialists' too, is in the question's trace."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from knesset.agents import specialist
    from knesset.tools import common

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    for module in (graph, specialist, common):
        monkeypatch.setattr(module, "tracer", lambda *_: tracer)
    run_graph(Script())
    spans = exporter.get_finished_spans()
    (root,) = [s for s in spans if s.name == "question"]
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert {"orchestrator.plan", "handoff.analyst", "handoff.researcher", "tool.sql_query",
            "tool.search_text", "orchestrator.verify"} <= {s.name for s in spans}  # fmt: skip
    assert root.attributes["path"] == " → ".join(CYCLE + ["verify", "finish"])
    assert root.attributes["verdict"] == "pass"


def test_traces_cli(monkeypatch):
    from types import SimpleNamespace as NS

    from typer.testing import CliRunner

    from knesset import tracing
    from knesset.cli import app

    spans = [
        {"name": "question", "start_time": "2026-11-24T10:00:00Z",
         "end_time": "2026-11-24T10:00:40Z", "context": {"trace_id": "t1"},
         "attributes": {"question": "כמה?", "path": "plan → finish", "verdict": "pass",
                        "cost_usd": 0.05, "steps": 4, "answer": "573 [1]"}},
        {"name": "agent.analyst", "attributes": {}},
    ]  # fmt: skip
    response = NS(json=lambda: {"data": spans}, raise_for_status=lambda: None)
    monkeypatch.setattr(tracing.httpx, "get", lambda *a, **k: response)
    out = CliRunner().invoke(app, ["traces", "--n", "5"])
    assert out.exit_code == 0, out.output
    assert "path plan → finish · verdict pass · $0.050 · steps 4 · 40 s · cites True" in out.output


def test_agents_config_names_every_agent_with_a_priced_model():
    from knesset.agents.models import agents
    from knesset.enrich.llm import PRICES

    config = agents()
    assert set(config) == {"orchestrator", "writer", "analyst", "researcher", "summarizer",
                           "verifier"}  # fmt: skip
    assert all(
        c["model"] in PRICES and c["effort"] in ("low", "medium", "high") for c in config.values()
    )


def test_tool_loops_cache_their_prompt():
    from knesset.agents.specialist import middleware

    names = [type(m).__name__ for m in middleware(8)]
    assert names == ["ModelCallLimitMiddleware", "AnthropicPromptCachingMiddleware"]
