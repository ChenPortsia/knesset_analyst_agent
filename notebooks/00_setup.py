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
# # 00 · Setup
# Connections, roles, Phoenix, compute device, and one sample log line + trace.

# %% tags=["parameters"]
STEP = "nb00_setup"

# %%
import psycopg

from knesset import log, tracing
from knesset.config import detect_device, settings
from knesset.db import connect
from knesset.nb import q, runs, trace_link

s = settings()
{"db": f"{s.db_host}:{s.db_port}/{s.db_name}", "phoenix": s.phoenix_url, "scope": s.knessets}

# %% [markdown]
# ## Database, schemas and roles

# %%
q("select version()").iloc[0, 0]

# %%
q("""select nspname as schema, pg_get_userbyid(nspowner) as owner from pg_namespace
     where nspname in ('raw','ops','text','marts','app') order by 1""")

# %%
q("""select rolname, rolconfig from pg_roles
     where rolname in ('etl','agent_ro','ui_app','phoenix') order by 1""")

# %%
q("select extname, extversion from pg_extension order by 1")

# %% [markdown]
# `agent_ro` is read-only with a 15 s statement timeout.

# %%
with connect("agent_ro") as conn:
    timeout = conn.execute("show statement_timeout").fetchone()[0]
    try:
        conn.execute("create table marts.should_fail (x int)")
        read_only = False
    except psycopg.Error as e:
        read_only = True
        print(type(e).__name__, "-", str(e).strip())
assert timeout == "15s" and read_only
timeout, read_only

# %% [markdown]
# ## Phoenix and compute device

# %%
import httpx

httpx.get(f"{s.phoenix_url}/healthz", timeout=5).status_code

# %%
detect_device(s.device)

# %% [markdown]
# ## Sample log line + trace

# %%
log.configure()
tracing.setup()
with log.run(STEP) as r:
    with tracing.tracer().start_as_current_span("child_step"):
        log.log.info("hello", item="sample")
    r.rows_out = 1
tracing.flush()
assert tracing.trace_in_phoenix(r.trace_id)
print(trace_link(r.trace_id))

# %%
import json

log_file = s.log_dir / STEP / sorted(p.name for p in (s.log_dir / STEP).iterdir())[-1]
[json.loads(x) for x in log_file.read_text().splitlines() if r.run_id in x]

# %%
runs(STEP, n=5)
