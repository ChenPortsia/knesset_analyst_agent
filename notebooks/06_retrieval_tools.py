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
# # 06 · Retrieval and tools
# Hybrid search (vector + keyword, rank fusion, neighbor windows) and every agent tool from
# PLAN.md §7, called on the real data with shape checks. Run natively with the `embed` extra
# (`uv run --extra embed jupytext --to ipynb --execute notebooks/06_retrieval_tools.py`);
# without it, search runs on keywords only.

# %% tags=["parameters"]
KNESSETS = [25, 26]
QUERIES = ['גיוס חרדים לצה"ל', "פסולת בניין", "יוקר המחיה", "החטופים בעזה", "רפורמה משפטית"]
TARGET_MS = 300

# %%
import json
import statistics
import time

import pandas as pd

from knesset.search import Filters, default_encoder, search
from knesset.tools.common import resolve_entity
from knesset.tools.lc import AGENT_TOOLS, tools_for
from knesset.tools.research import facets, get_document, search_text, share_of_words
from knesset.tools.sql import describe_schema, make_chart, sql_query
from knesset.tools.summary import get_document_summary
from knesset.tools.verify import get_chunks, rerun_sql, verdict

encode = default_encoder()
print("hybrid" if encode else "keywords only (no embed extra)")
F = {"knessets": KNESSETS}


def check(result: dict, *keys: str) -> dict:
    """Every tool returns JSON with an evidence list and its own keys."""
    assert "evidence" in result and all(k in result for k in keys), sorted(result)
    json.dumps(result, ensure_ascii=False)
    return result


# %% [markdown]
# ## Search latency
# Warm runs (the model loaded once), hybrid search with default neighbors, k = 10.

# %%
search(QUERIES[0], Filters(knessets=KNESSETS), encode=encode)  # warm-up
timings = []
for query in QUERIES:
    for _ in range(3):
        started = time.perf_counter()
        windows = search(query, Filters(knessets=KNESSETS), k=10, encode=encode)
        timings.append({"query": query, "ms": (time.perf_counter() - started) * 1000,
                        "windows": len(windows)})  # fmt: skip
timings = pd.DataFrame(timings)
median = statistics.median(timings.ms)
print(f"median {median:.0f} ms, p90 {timings.ms.quantile(0.9):.0f} ms (target < {TARGET_MS} ms)")
assert median < TARGET_MS
timings.groupby("query").ms.median().round()

# %% [markdown]
# ## Search results
# The best hit per query, with its neighbors.

# %%
for query in QUERIES[:3]:
    w = search(query, Filters(knessets=KNESSETS), k=1, encode=encode)[0]
    print(f"\n== {query}  →  {w.group_type} · {w.date} · {w.url}")
    for c in w.chunks:
        print(f"  [{c.mark:6}] {' '.join(c.text.split())[:200]}")

# %% [markdown]
# ## Every tool

# %%
committee = check(resolve_entity("ועדת הכספים", types=["committee"], knesset=25), "candidates")
committee_id = committee["candidates"][0]["id"]
person = check(resolve_entity("בנימין נתניהו", types=["person"]), "candidates")
pd.DataFrame(committee["candidates"] + person["candidates"])

# %% [markdown]
# Filtered search latency: one committee (exact distances over its chunks, not the index).

# %%
cf = Filters(knessets=KNESSETS, committee_ids=[committee_id])
search(QUERIES[0], cf, encode=encode)  # warm-up
ms = []
for query in QUERIES:
    started = time.perf_counter()
    search(query, cf, k=10, encode=encode)
    ms.append((time.perf_counter() - started) * 1000)
print(f"committee {committee_id}: median {statistics.median(ms):.0f} ms, max {max(ms):.0f} ms")
assert statistics.median(ms) < TARGET_MS

# %%
found = check(search_text("תקציב המדינה", F | {"committee_ids": [committee_id]}, k=3), "results")
hit = found["results"][0]
chunk_id = next(c["chunk_id"] for c in hit["chunks"] if c["mark"] == "hit")
print(hit["doc_type"], hit["date"], hit["url"], [c["mark"] for c in hit["chunks"]])

# %%
doc = check(get_document(hit["doc_id"], around_chunk=chunk_id, window=1), "document", "chunks")
again = check(get_chunks([chunk_id], neighbors=1), "results")
doc["document"]

# %%
check(facets("speaker", F | {"committee_ids": [committee_id]}, query="תקציב"), "counts")
pd.DataFrame(facets("faction", F, query="יוקר המחיה")["counts"]).head(10)

# %% [markdown]
# Share of the spoken words per agenda item in the Finance Committee, and per faction.

# %%
by_section = check(share_of_words("section", F | {"committee_ids": [committee_id]}), "shares")
print(f"{by_section['total_words']:,} words")
pd.DataFrame(by_section["shares"]).head(10)

# %%
pd.DataFrame(share_of_words("faction", F | {"committee_ids": [committee_id]})["shares"]).head(10)

# %%
summary = check(get_document_summary(hit["doc_id"]), "document", "sections")
pd.DataFrame(summary["sections"])

# %%
tables = check(describe_schema("votes הצבעות"), "tables")["tables"]
print([t["table"] for t in tables])
result = check(
    sql_query("""select knesset_num, count(*) as bills, count(*) filter (where is_passed) as passed
                 from marts.fct_bill where knesset_num >= 20 group by 1 order by 1"""),
    "columns",
    "rows",
)
assert rerun_sql(result["evidence"][0]["ref"])["rows"] == result["rows"]
chart = check(make_chart(result, {"kind": "bar", "x": "knesset_num", "y": "passed"}), "chart")
pd.DataFrame(result["rows"], columns=result["columns"])

# %%
check(verdict("pass"), "status")
print({agent: [t.name for t in tools_for(agent)] for agent in AGENT_TOOLS})
