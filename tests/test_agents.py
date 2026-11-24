"""The agent graph end to end with a fake Claude client (no paid calls)."""

import json
import threading
from types import SimpleNamespace as NS

import pytest
from test_search import chunks, encode  # noqa: F401
from test_tools import data  # noqa: F401  (search fixture + marts + sections)

from knesset.agents import graph
from knesset.agents.contracts import AgentTask, Evidence, Finding
from knesset.agents.llm import Budget, json_schema
from knesset.agents.specialist import keep_seen, run
from knesset.config import settings

SQL = "select count(*) as people from marts.dim_person"


def block(**kw):
    return NS(**kw)


class FakeClaude:
    """Answers like each agent would, chosen by the system prompt. `verdicts` scripts the
    verifier ("pass", "unsupported" or "missing"); `cite` is the chunk id the researcher claims
    (None: the first one it found); `answer` is the draft's text."""

    def __init__(self, verdicts=("pass",), cite=None, plan=None, answer=None):
        self.verdicts, self.cite, self.plan = list(verdicts), cite, plan
        self.answer = answer or "There are 3 people [1]; the train is late [2]."
        self.prompts: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.beta = NS(messages=NS(create=self.create))

    def reply(self, content, stop="end_turn"):
        usage = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                   cache_creation_input_tokens=0)  # fmt: skip
        return NS(content=content, stop_reason=stop, usage=usage, model="claude-sonnet-5-5")

    def final(self, obj):
        return self.reply([block(type="text", text=json.dumps(obj, ensure_ascii=False))])

    def call(self, name, **args):
        return self.reply(
            [block(type="tool_use", id=f"tu_{name}", name=name, input=args)], "tool_use"
        )

    def create(self, system, messages, **kw):
        agent = system.split("# ")[2].split(" ")[0]  # the agent prompt's title
        self.cached = getattr(self, "cached", []) + [(agent, "tools" in kw, "cache_control" in kw)]
        self.fallbacks = getattr(self, "fallbacks", []) + [(kw["model"], "fallbacks" in kw)]
        results = [b for m in messages if m["role"] == "user" and isinstance(m["content"], list)
                   for b in m["content"]]  # fmt: skip
        with self.lock:
            self.prompts.append((agent, messages[0]["content"]))
        if agent == "Orchestrator" and "planning" in system and "missing" in messages[0]["content"]:
            return self.final(
                {
                    "clarification": "",
                    "tasks": [
                        {"id": "t3", "agent": "researcher", "instruction": "more", "depends_on": []}
                    ],
                }
            )
        if agent == "Orchestrator" and "planning" in system:
            return self.final(self.plan or {"clarification": "", "tasks": [
                {"id": "t1", "agent": "analyst", "instruction": "count people", "depends_on": []},
                {"id": "t2", "agent": "researcher", "instruction": "train delays",
                 "depends_on": ["t1"]},
            ]})  # fmt: skip
        if agent == "Orchestrator" and "verifier found these issues" in messages[0]["content"]:
            return self.final({"answer": "There are 3 people [1].", "caveats": "Removed the 4."})
        if agent == "Orchestrator":  # the draft
            return self.final({"answer": self.answer, "caveats": ""})
        if agent == "Analyst":
            if not results:
                return self.call("sql_query", sql=SQL)
            evidence = [{"type": "sql", "ref": SQL, "url": None}]
            return self.final({"findings": [{"claim": "3 people", "confidence": "high",
                                             "evidence": evidence}], "gaps": ""})  # fmt: skip
        if agent == "Researcher":
            if not results:
                return self.call("search_text", query="רכבת", filters={"committee_ids": [7]}, k=1)
            hit = json.loads(results[0]["content"])["evidence"][0]
            ref = str(self.cite or hit["ref"])
            return self.final({"findings": [{"claim": "the train is late", "confidence": "high",
                               "evidence": [{"type": "chunk", "ref": ref, "url": hit["url"]}]}],
                               "gaps": ""})  # fmt: skip
        if agent == "Verifier":
            if not results:
                return self.call("rerun_sql", sql=SQL)
            kind = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            if kind == "pass":
                return self.final({"status": "pass", "issues": []})
            issue = {"claim": "4 people", "problem": "the count is 3, not 4", "kind": kind}
            return self.final({"status": "fail", "issues": [issue]})
        raise AssertionError(f"unexpected agent {agent!r}")


def run_graph(api, **kw):
    events = list(graph.ask("כמה אנשים יש, ומה עם הרכבת?", api=api, **kw))
    return [name for name, _ in events[:-1]], events[-1][1]


def test_json_schema_is_strict():
    schema = json_schema(Finding)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["claim", "evidence", "confidence"]
    assert schema["$defs"]["Evidence"]["required"] == ["type", "ref", "url"]


def test_keep_seen_drops_invented_evidence():
    evidence = [Evidence(type="chunk", ref="7"), Evidence(type="chunk", ref="999")]
    f = Finding(claim="x", confidence="high", evidence=evidence)
    (kept,) = keep_seen([f], {("chunk", "7")})
    assert [e.ref for e in kept.evidence] == ["7"] and kept.confidence == "high"
    (gone,) = keep_seen([f], set())
    assert gone.evidence == [] and gone.confidence == "low"


def test_specialist_cites_only_what_it_saw(data):  # noqa: F811
    budget = Budget(1.0, 12, 90)
    task = AgentTask(task_id="t1", agent="researcher", instruction="train")
    seen = run(task, budget, FakeClaude())
    assert seen.findings[0].confidence == "high" and seen.findings[0].evidence[0].url
    invented = run(task, budget, FakeClaude(cite=123456789))
    assert invented.findings[0].evidence == [] and invented.findings[0].confidence == "low"
    assert budget.usage.calls == 4 and budget.steps == 2


def test_full_graph(data):  # noqa: F811
    api = FakeClaude()
    nodes, result = run_graph(api)
    assert nodes == ["plan", "delegate", "draft", "verify", "finish"]
    assert result["answer"] == "There are 3 people [1]; the train is late [2]."
    assert [s["type"] for s in result["sources"]] == ["sql", "chunk"]
    assert result["verdict"] == {"status": "pass", "issues": []}
    # the researcher depends on the analyst: it got the analyst's findings as context
    researcher = {p for a, p in api.prompts if a == "Researcher"}  # one task, two calls
    assert len(researcher) == 1 and "3 people" in researcher.pop()
    u = result["usage"]
    assert u["steps"] == 5 and u["calls"] == 8  # plan, 2+2 specialists, draft, 2 verifier
    assert u["cost_usd"] == pytest.approx(8 * (1000 * 2 + 100 * 10) / 1e6)
    assert len(result["trace_id"]) == 32
    # only tool loops write the cache (their next call reads it); the one-shot draft does not
    assert all(tools == cached for _, tools, cached in api.cached)
    assert ("Orchestrator", False, False) in api.cached
    # Haiku 5.5 (the verifier) has no server-side fallback: the parameter is not sent
    assert ("claude-haiku-5-5", False) in api.fallbacks
    assert ("claude-sonnet-5-5", True) in api.fallbacks


def test_unsupported_claims_are_revised_without_searching(data):  # noqa: F811
    nodes, result = run_graph(FakeClaude(verdicts=["unsupported"]))
    assert nodes == ["plan", "delegate", "draft", "verify", "revise", "finish"]
    assert result["answer"].startswith("There are 3 people [1].")
    assert "Removed the 4." in result["answer"] and "Not fully verified" not in result["answer"]


def test_missing_information_is_delegated_once(data):  # noqa: F811
    nodes, result = run_graph(FakeClaude(verdicts=["missing", "pass"]))
    assert nodes == ["plan", "delegate", "draft", "verify", "plan", "delegate", "draft",
                     "verify", "finish"]  # fmt: skip
    assert [r["task_id"] for r in result["results"]] == ["t1", "t2", "t3"]
    # still missing after the one re-plan: revise from what is there
    nodes, _ = run_graph(FakeClaude(verdicts=["missing", "missing"]))
    assert nodes[-3:] == ["verify", "revise", "finish"] and nodes.count("plan") == 2


def test_sql_only_answer_is_checked_in_code(data):  # noqa: F811
    task = {"id": "t1", "agent": "analyst", "instruction": "count people", "depends_on": []}
    plan = {"clarification": "", "tasks": [task]}
    api = FakeClaude(plan=plan, answer="There are 3 people [1].")
    nodes, result = run_graph(api)
    assert result["verdict"] == {"status": "pass", "issues": [], "checked": "sql in code"}
    assert not any(a == "Verifier" for a, _ in api.prompts)  # no model call for the check
    # a number the SQL did not return goes to the verifier (ranks up to 20 are allowed)
    api = FakeClaude(plan=plan, answer="There are 40 people [1].", verdicts=["unsupported"])
    nodes, _ = run_graph(api)
    assert any(a == "Verifier" for a, _ in api.prompts) and "revise" in nodes


def test_numbers_normalize():
    assert graph.numbers("1,154 ישיבות [3], 91.5%, ב-24.3.2025") == {
        "1154", "91.5", "24", "3", "2025"
    }  # fmt: skip


def test_clarification(data):  # noqa: F811
    api = FakeClaude(plan={"clarification": "באיזו ועדה?", "tasks": []})
    nodes, result = run_graph(api)
    assert nodes == ["plan", "finish"] and result["answer"] == "באיזו ועדה?"


def test_limits_end_early_with_what_is_known(data, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENT_MAX_COST_USD", "0.005")  # spent after two calls
    settings.cache_clear()
    nodes, result = run_graph(FakeClaude())
    assert nodes[-1] == "finish" and "Stopped early: cost limit" in result["answer"]
    monkeypatch.setenv("AGENT_MAX_COST_USD", "1")
    monkeypatch.setenv("AGENT_MAX_STEPS", "1")
    settings.cache_clear()
    _, result = run_graph(FakeClaude())
    assert "step limit" in result["answer"]


def test_ask_cli(data, monkeypatch):  # noqa: F811
    from typer.testing import CliRunner

    from knesset.agents import llm
    from knesset.cli import app

    monkeypatch.setattr(llm, "client", lambda: FakeClaude())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    settings.cache_clear()
    out = CliRunner().invoke(app, ["ask", "כמה אנשים יש?"])
    assert out.exit_code == 0, out.output
    for line in ("plan   t1 → analyst", "plan   t2 → researcher (after t1)",
                 "agent  t1 analyst: 1 findings", "verify pass", "There are 3 people [1]",
                 "[1] sql select count(*)", "8 calls · 5 steps"):  # fmt: skip
        assert line in out.output, out.output
    assert "tool_call" not in out.output  # tool calls go to the log file, not the terminal


def test_one_trace_per_question(data, monkeypatch):  # noqa: F811
    """Every span of a question, the parallel specialists' too, is in the question's trace."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    from knesset.agents import graph as g
    from knesset.agents import specialist
    from knesset.tools import common

    for module in (g, specialist, common):
        monkeypatch.setattr(module, "tracer", lambda *_: tracer)
    run_graph(FakeClaude())
    spans = exporter.get_finished_spans()
    (root,) = [s for s in spans if s.name == "question"]
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    names = {s.name for s in spans}
    assert {"orchestrator.plan", "orchestrator.delegate", "handoff.analyst", "agent.analyst",
            "handoff.researcher", "tool.sql_query", "tool.search_text", "orchestrator.verify",
            "orchestrator.finish"} <= names  # fmt: skip
    assert root.attributes["path"] == "plan → delegate → draft → verify → finish"
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
