"""The eval set and the model matrix, with fake models (no paid calls)."""

import pytest
import yaml
from test_agents import Script
from test_search import chunks, encode  # noqa: F401
from test_tools import data  # noqa: F401

from knesset import evals
from knesset.agents.models import factory, price
from knesset.config import settings


def test_grade_sql_needs_every_value_of_the_reference_row():
    q = {"kind": "sql"}
    assert evals.grade(q, "1,154 ישיבות [1]", [{"n": 1}], ["1154"])["score"] == 1
    right = evals.grade(q, "ועדת הכספים, עם 1154 ישיבות [1]", [{"n": 1}], ["ועדת הכספים", "1154"])
    assert right == {"score": 1.0, "missed": [], "cited": True}
    wrong = evals.grade(q, "ועדת הכספים: 1153", [], ["ועדת הכספים", "1154"])
    assert wrong == {"score": 0.0, "missed": ["1154"], "cited": False}


def test_grade_text_counts_matched_groups():
    q = {"kind": "text", "expect": [["ביטן", "Bitan"], ["בליאק"], ["סמוטריץ'"]]}
    graded = evals.grade(q, "MK Bitan and בליאק spoke most [1].", [{"n": 1}])
    assert graded["score"] == pytest.approx(2 / 3) and graded["missed"] == ["סמוטריץ'"]


def test_factory_replaces_only_named_agents(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    settings.cache_clear()
    models = factory({"analyst": "claude-sonnet-5-5"})
    assert models("analyst").model == "claude-sonnet-5-5"
    assert models("researcher").model == "claude-haiku-5-5"  # the default
    assert price("gpt-6-luna-2026-09-22") == (0.10, 0.50, 0.01)
    medium = factory({"writer": {"model": "claude-sonnet-5-5", "effort": "medium"}})(
        "writer", "low"
    )
    assert medium.output_config == {"effort": "medium"}  # a config can change the effort too


def test_openai_models(monkeypatch):
    pytest.importorskip("langchain_openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    settings.cache_clear()
    from knesset.agents.models import chat_model

    luna = chat_model("analyst", "low", "gpt-6-luna")
    assert type(luna).__name__ == "ChatOpenAI" and luna.reasoning == {"effort": "low"}
    assert luna.use_responses_api  # function calling with reasoning needs the Responses API


def test_matrix_runs_grades_and_reports(data, tmp_path, monkeypatch):  # noqa: F811
    questions = [
        {"id": "people", "kind": "sql", "question": "כמה אנשים?",
         "sql": "select count(*) from marts.dim_person"},
        {"id": "train", "kind": "text", "question": "מה עם הרכבת?",
         "expect": [["train"], ["bus"]]},
    ]  # fmt: skip
    (tmp_path / "q.yaml").write_text(yaml.safe_dump(questions, allow_unicode=True))
    (tmp_path / "c.yaml").write_text("baseline: {}\ncheap:\n  analyst: claude-haiku-5-5\n")
    monkeypatch.setattr(evals, "QUESTIONS", tmp_path / "q.yaml")
    monkeypatch.setattr(evals, "CONFIGS", tmp_path / "c.yaml")
    seen = []
    out = tmp_path / "run.jsonl"
    rows = list(evals.run(["baseline", "cheap"], 5.0, models=lambda names: seen.append(names)
                          or Script().models, out=out))  # fmt: skip
    assert seen == [{}, {}, {"analyst": "claude-haiku-5-5"}, {"analyst": "claude-haiku-5-5"}]
    assert [(r["config"], r["id"], r["score"]) for r in rows] == [
        ("baseline", "people", 1.0), ("baseline", "train", 0.5),
        ("cheap", "people", 1.0), ("cheap", "train", 0.5),
    ]  # fmt: skip
    assert len(out.read_text().splitlines()) == 4
    (line,) = [s for s in evals.summary(rows) if s["config"] == "cheap"]
    assert line["sql_right"] == "1/1" and line["text_score"] == 0.5 and line["cited"] == "2/2"
    assert "| people | cheap | 1.00 | True |" in evals.markdown(rows)
    # the cap: no question starts whose worst case would pass it
    fresh = tmp_path / "capped.jsonl"
    skipped = list(evals.run(["baseline"], 0.1, models=lambda n: Script().models, out=fresh))
    assert all("skipped" in r for r in skipped)


def test_eval_goes_on_after_an_error_and_resumes(data, tmp_path, monkeypatch):  # noqa: F811
    questions = [{"id": "people", "kind": "sql", "question": "כמה אנשים?",
                  "sql": "select count(*) from marts.dim_person"}]  # fmt: skip
    (tmp_path / "q.yaml").write_text(yaml.safe_dump(questions, allow_unicode=True))
    (tmp_path / "c.yaml").write_text("baseline: {}\nbad: {}\n")
    monkeypatch.setattr(evals, "QUESTIONS", tmp_path / "q.yaml")
    monkeypatch.setattr(evals, "CONFIGS", tmp_path / "c.yaml")

    def models(names):
        if calls.append(names) or len(calls) == 2:
            raise RuntimeError("broken model")
        return Script().models

    calls: list = []
    out = tmp_path / "run.jsonl"
    rows = list(evals.run(["baseline", "bad"], 5.0, models=models, out=out))
    assert [r["score"] for r in rows] == [1.0, 0.0] and "broken model" in rows[1]["error"]
    again = list(evals.run(["baseline", "bad"], 5.0, models=models, out=out))
    assert again[0] == rows[0] and len(calls) == 3  # resumed: only the failed one ran again
    assert again[1]["score"] == 1.0 and not again[1]["error"]


def test_eval_set_is_well_formed():
    questions = evals.load(evals.QUESTIONS)
    assert len({q["id"] for q in questions}) == len(questions) == 26
    for q in questions:
        assert q["kind"] in ("sql", "text") and q["question"]
        assert q.get("sql") if q["kind"] == "sql" else q.get("expect"), q["id"]
    assert set(evals.load(evals.CONFIGS)) >= {"baseline", "sonnet-specialists", "all-haiku"}


def test_follow_up_is_asked_in_the_same_thread(data, monkeypatch):  # noqa: F811
    asked = []
    real = evals.ask

    def ask(question, **kw):
        asked.append((question, kw["thread_id"]))
        yield from real(question, **kw)

    monkeypatch.setattr(evals, "ask", ask)
    script = Script()
    result = evals.answer_one("ומי מהם?", "u", lambda: script.models, ["כמה אנשים?"])
    assert [q for q, _ in asked] == ["כמה אנשים?", "ומי מהם?"] and len({t for _, t in asked}) == 1
    assert result["usage"]["cost_usd"] == pytest.approx(2 * 9 * 3000 / 1e6)  # both questions
    plans = [p for a, p in script.prompts if a == "planner"]
    assert "Earlier in this conversation" in plans[-1]


def test_truth_keeps_columns_that_share_a_name(data):  # noqa: F811
    """Two count(*) columns are both called "count"; the grader must check both."""
    sql = "select count(*) filter (where person_id = 1), count(*) from marts.dim_person"
    assert evals.truth(sql) == ["1", "3"]
