# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # 07 · Agents
# **Paid** (Claude API). Each specialist alone, the verifier on planted errors, then the full
# graph on 10 mixed questions. Every step checks the notebook's cap first: it never starts a
# step whose worst case (`AGENT_MAX_COST_USD` per question) would pass `MAX_NOTEBOOK_USD`.
# Run natively: `uv run --extra embed jupytext --to ipynb --execute notebooks/07_agents.py`.

# %% tags=["parameters"]
KNESSETS = [25, 26]
MAX_NOTEBOOK_USD = 5.0
QUESTIONS = [
    "כמה הצעות חוק עברו בכנסת ה-25?",
    "Which committees held the most sessions in the 25th Knesset?",
    "מה נאמר בוועדת החוץ והביטחון על גיוס חרדים?",
    "How did the factions vote on the 2025 state budget law?",
    "כמה מזמן הדיונים של ועדת הכספים הוקדש לחוק התקציב לשנת 2025?",
    "Which MKs spoke most about the cost of living (יוקר המחיה)?",
    "אילו ארגונים הופיעו הכי הרבה בוועדת העבודה והרווחה?",
    "What were the main arguments in the debate on construction waste (פסולת בניין)?",
    "אילו משרדי ממשלה קיבלו הכי הרבה שאילתות בכנסת ה-25?",
    "מה אמרו חברי הכנסת על החטופים בעזה במליאה?",
]

# %%
import time

import pandas as pd

from knesset import tracing
from knesset.agents.contracts import AgentTask
from knesset.agents.graph import ask
from knesset.agents.llm import Budget
from knesset.agents.specialist import run
from knesset.config import settings

tracing.setup()
tracing.instrument_llm()
s = settings()
spent = 0.0


def room(worst: float) -> bool:
    """Whether a step that may cost `worst` fits under the notebook's cap."""
    ok = spent + worst <= MAX_NOTEBOOK_USD
    if not ok:
        print(f"skipped: ${spent:.2f} spent, a step may cost ${worst:.2f}, cap ${MAX_NOTEBOOK_USD}")
    return ok


def specialist(agent: str, instruction: str, context: str = ""):
    global spent
    budget = Budget(s.agent_max_cost_usd, s.agent_max_steps, s.agent_timeout_s)
    started = time.monotonic()
    task = AgentTask(task_id="smoke", agent=agent, instruction=instruction, context=context,
                     filters={"knessets": KNESSETS})  # fmt: skip
    result = run(task, budget)
    spent += budget.usage.cost_usd
    u, seconds = budget.usage, time.monotonic() - started
    print(f"{agent}: {len(result.findings)} findings, ${u.cost_usd:.3f}, {u.calls} calls, "
          f"{seconds:.0f} s, error={result.error}")  # fmt: skip
    return result


# %% [markdown]
# ## 1 · Each specialist alone

# %%
smoke = {}
for agent, instruction in [
    ("analyst", "How many bills passed in Knesset 25? One number, with the SQL."),
    ("researcher", "Find what MKs said about the cost of living (יוקר המחיה) in the Economic "
                   "Affairs Committee in Knesset 25: three statements with speaker and date."),
    ("summarizer", "Main arguments for and against the construction waste (פסולת בניין) "
                   "legislation in Knesset 25."),
]:  # fmt: skip
    if room(s.agent_max_cost_usd):
        smoke[agent] = specialist(agent, instruction)
        assert smoke[agent].error is None and smoke[agent].findings, smoke[agent]
        assert any(f.evidence for f in smoke[agent].findings), "no finding cites evidence"

pd.DataFrame(
    [
        {
            "agent": a,
            "claim": f.claim[:160],
            "evidence": len(f.evidence),
            "confidence": f.confidence,
        }
        for a, r in smoke.items()
        for f in r.findings
    ]  # fmt: skip
)

# %% [markdown]
# ## 2 · The verifier catches planted errors
# One correct draft built from the analyst's finding, and two with planted errors: a changed
# number, and a claim no source supports.

# %%
fact = smoke["analyst"].findings[0]
sql = next(e.ref for e in fact.evidence if e.type == "sql")
number = next(int(t) for t in fact.claim.replace(",", "").split() if t.isdigit())
sources = f"[1] sql {sql}"
drafts = {
    "correct": (f"{number} bills passed in Knesset 25 [1].", "pass"),
    "wrong number": (f"{number * 2 + 7} bills passed in Knesset 25 [1].", "fail"),
    "invented claim": (f"{number} bills passed in Knesset 25 [1], most of them proposed by the "
                       "opposition.", "fail"),
}  # fmt: skip
verdicts = []
for name, (draft, expected) in drafts.items():
    if not room(s.agent_max_cost_usd):
        break
    r = specialist("verifier", "Check this draft answer against its sources.",
                   f"Question: How many bills passed in Knesset 25?\n\nDraft:\n{draft}\n\n"
                   f"Findings:\n- {fact.claim} [1]\n\nSources:\n{sources}")  # fmt: skip
    verdicts.append({"draft": name, "expected": expected, "got": (r.verdict or {}).get("status"),
                     "issues": (r.verdict or {}).get("issues")})  # fmt: skip
verdicts = pd.DataFrame(verdicts)
assert (verdicts.expected == verdicts.got).all(), verdicts
verdicts

# %% [markdown]
# ## 3 · The full graph on 10 mixed questions

# %%
rows = []
for question in QUESTIONS:
    if not room(s.agent_max_cost_usd):
        break
    *steps, (_, result) = ask(question, user_id="notebook-07", filters={"knessets": KNESSETS})
    u = result["usage"]
    spent += u["cost_usd"]
    rows.append({
        "question": question, "nodes": " → ".join(n for n, _ in steps),
        "verdict": (result["verdict"] or {}).get("status"), "sources": len(result["sources"]),
        "cites": "[1]" in result["answer"], "cost": round(u["cost_usd"], 3), "calls": u["calls"],
        "steps": u["steps"], "seconds": u["seconds"], "trace_id": result["trace_id"],
        "answer": result["answer"],
    })  # fmt: skip
    print(f"\n### {question}\n{result['answer']}\n${u['cost_usd']:.3f} · {u['seconds']} s")
runs = pd.DataFrame(rows)
print(f"\nspent ${spent:.2f} in total")
runs.drop(columns=["answer", "trace_id"])

# %%
assert (runs.cites & (runs.sources > 0)).mean() >= 0.8, "most answers must cite sources"
assert (runs.steps <= s.agent_max_steps).all() and (runs.seconds <= s.agent_timeout_s + 30).all()
assert (runs.cost <= s.agent_max_cost_usd + 0.1).all()

# %% [markdown]
# ## 4 · Every question is a trace in Phoenix
# With a span per agent turn and per handoff (`handoff.analyst`, `agent.analyst`, …).

# %%
tracing.flush()
found = [tracing.trace_in_phoenix(t) for t in runs.trace_id]
print(f"{sum(found)}/{len(found)} traces in Phoenix; e.g. {tracing.trace_link(runs.trace_id[0])}")
assert all(found)
