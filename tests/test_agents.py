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
    verifier; `cite` is the chunk id the researcher claims (None: the first one it found)."""

    def __init__(self, verdicts=("pass",), cite=None, plan=None):
        self.verdicts, self.cite, self.plan = list(verdicts), cite, plan
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
        results = [b for m in messages if m["role"] == "user" and isinstance(m["content"], list)
                   for b in m["content"]]  # fmt: skip
        with self.lock:
            self.prompts.append((agent, messages[0]["content"]))
        if agent == "Orchestrator" and "planning" in system:
            return self.final(self.plan or {"clarification": "", "tasks": [
                {"id": "t1", "agent": "analyst", "instruction": "count people", "depends_on": []},
                {"id": "t2", "agent": "researcher", "instruction": "train delays",
                 "depends_on": ["t1"]},
            ]})  # fmt: skip
        if agent == "Orchestrator":  # the draft
            return self.final({"answer": "There are 3 people [1]; the train is late [2].",
                               "caveats": ""})  # fmt: skip
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
            status = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            issues = [] if status == "pass" else ["the count is 3, not 4"]
            return self.final({"status": status, "issues": issues})
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


def test_verifier_fail_replans_once_then_caveats(data):  # noqa: F811
    nodes, result = run_graph(FakeClaude(verdicts=["fail", "pass"]))
    assert nodes.count("plan") == 2 and result["verdict"]["status"] == "pass"
    assert "⚠" not in result["answer"]
    nodes, result = run_graph(FakeClaude(verdicts=["fail"]))
    assert nodes.count("verify") == 2  # re-delegated once, then answered with caveats
    assert "Not fully verified: the count is 3, not 4" in result["answer"]


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
