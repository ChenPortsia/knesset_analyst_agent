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
# # 01 · OData load
# Run after a full load (`make sync`). Needs an Israeli IP: it queries knesset.gov.il live.
#
# Counts vs live `$count`, sync state, throughput, raw samples, a kill-and-restart resume
# test, and a timed incremental run.

# %% tags=["parameters"]
RESUME_TABLE = "KNS_CommitteeSession"  # > 100k rows, so it is partitioned
KILL_AFTER_S = 20
RUN_RESUME_TEST = True
RUN_INCREMENTAL = True
MAX_DIFF_PCT = 0.5

# %%
import asyncio
import json
import time

import pandas as pd
from IPython.display import display

from knesset import log
from knesset.config import settings
from knesset.nb import q, runs
from knesset.odata import OData
from knesset.sync import sync

log.configure()

# %% [markdown]
# ## Live metadata

# %%
async with OData() as api:
    sets = await api.metadata()
pd.DataFrame(
    [
        (es.name, es.base_url.rsplit("/", 1)[-1], es.has_last_updated, len(es.columns))
        for es in sets
    ],
    columns=["set", "service", "has_last_updated", "columns"],
)

# %% [markdown]
# ## Row counts vs live `$count`


# %%
async def live_counts() -> dict[str, int | None]:
    async with OData() as api:
        counts = await asyncio.gather(*(api.count(es) for es in sets))
    return dict(zip([es.name for es in sets], counts, strict=True))


live = await live_counts()
existing = set(q("select tablename from pg_tables where schemaname = 'raw'").tablename)


def raw_count(table: str) -> int:
    return int(q(f'select count(*) from raw."{table}"').iloc[0, 0]) if table in existing else 0


counts = pd.DataFrame(
    [(es.name, live[es.name], raw_count(es.table)) for es in sets], columns=["set", "live", "raw"]
)
counts["diff_pct"] = ((counts.raw - counts.live).abs() / counts.live.clip(lower=1) * 100).round(3)
counts.sort_values("live", ascending=False)

# %%
bad = counts[counts.live.notna() & (counts.diff_pct > MAX_DIFF_PCT)]
print(f"{len(counts)} sets, {int(counts.raw.sum()):,} raw rows, {len(bad)} off > {MAX_DIFF_PCT}%")
assert bad.empty, bad

# %% [markdown]
# ## Sync state

# %%
q("""select table_name, state->>'mode' as mode, jsonb_array_length(state->'partitions') as parts,
            watermark, last_id, updated_at
     from ops.sync_state order by table_name""")

# %% [markdown]
# ## Throughput

# %%
runs("sync", n=10)[
    ["run_id", "started_at", "finished_at", "status", "rows_in", "rows_out", "params"]
]

# %%
lines = [
    json.loads(x)
    for f in sorted((settings().log_dir / "sync").glob("*.jsonl"))
    for x in f.read_text().splitlines()
]
tables = pd.DataFrame([x for x in lines if x["event"] == "table_synced"])
if not tables.empty:
    tables["pages_per_s"] = (tables.requests / tables.seconds.clip(lower=0.1)).round(1)
    display(
        tables.sort_values("timestamp")
        .groupby("table")
        .last()
        .sort_values("seconds", ascending=False)
        .head(15)
    )

# %% [markdown]
# ## Raw samples

# %%
for table in ["kns_person", "kns_bill", "kns_plenumvoteresult", "kns_documentcommitteesession"]:
    display(q(f'select id, last_updated, doc from raw."{table}" order by random() limit 2'))

# %% [markdown]
# ## Kill-and-restart resume test
# Restart a full load of one table, cancel it after `KILL_AFTER_S` seconds (checkpoints persist),
# then resume. Expect no duplicates and a count matching the live count.

# %%
if RUN_RESUME_TEST:
    table = RESUME_TABLE.lower()
    try:
        await asyncio.wait_for(sync([RESUME_TABLE], full=True), KILL_AFTER_S)
    except TimeoutError:
        print("killed")
    parts = q(f"select state->'partitions' p from ops.sync_state where table_name = '{table}'").p[0]
    print("partitions done at kill:", sum(p.get("done", False) for p in parts), "/", len(parts))

    started = time.monotonic()
    await sync([RESUME_TABLE])
    print(f"resumed in {time.monotonic() - started:.0f}s")
    total, distinct = q(f'select count(*), count(distinct id) from raw."{table}"').iloc[0]
    async with OData() as api:
        live_n = await api.count(next(es for es in sets if es.name == RESUME_TABLE))
    print(f"rows {total:,}, distinct {distinct:,}, live {live_n:,}")
    assert total == distinct
    assert abs(total - live_n) / live_n * 100 <= MAX_DIFF_PCT

# %% [markdown]
# ## Incremental run (all tables)

# %%
if RUN_INCREMENTAL:
    started = time.monotonic()
    written = await sync()
    minutes = (time.monotonic() - started) / 60
    print(f"incremental: {sum(written.values()):,} rows written in {minutes:.1f} min")
    display(pd.Series(written).loc[lambda s: s > 0].sort_values(ascending=False))
