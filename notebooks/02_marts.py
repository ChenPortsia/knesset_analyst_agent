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
# # 02 · Marts
# The dbt marts built from `raw.*`: row counts, dbt test results, data checks and sample
# analyses. Run after `make dbt` (or set `RUN_DBT = True`).

# %% tags=["parameters"]
KNESSET = 25
RUN_DBT = False

# %%
import json
import subprocess

import pandas as pd

from knesset.cli import DBT_DIR
from knesset.nb import q

if RUN_DBT:
    subprocess.run(["uv", "run", "knesset", "dbt", "build"], check=True)

# %% [markdown]
# ## Row counts

# %%
marts = q(
    "select table_name from information_schema.tables where table_schema = 'marts' order by 1"
)
counts = pd.DataFrame(
    [(t, q(f"select count(*) from marts.{t}").iloc[0, 0]) for t in marts.table_name],
    columns=["mart", "rows"],
)
counts

# %%
assert (counts.rows > 0).all(), counts[counts.rows == 0]

# %% [markdown]
# ## dbt test results (last build)

# %%
results = json.loads((DBT_DIR / "target" / "run_results.json").read_text())["results"]
tests = pd.DataFrame(
    [
        (r["unique_id"].split(".")[2], r["status"], r.get("failures"))
        for r in results
        if r["unique_id"].startswith("test.")
    ],
    columns=["test", "status", "failures"],
)
print(tests.status.value_counts().to_dict())
tests[tests.status != "pass"].sort_values("failures", ascending=False)

# %%
assert not tests.status.isin(["fail", "error"]).any()

# %% [markdown]
# ## Data checks
# Faction at vote time: share of per-MK vote results matched to a faction membership.

# %%
q("""select count(*) as results, round(avg((faction_id is not null)::int) * 100, 2) as pct_matched
     from marts.fct_vote_result""")

# %%
q("""select date_part('year', vote_datetime)::int as year,
            round(avg((faction_id is not null)::int) * 100, 1) as pct_matched
     from marts.fct_vote_result group by 1 order by 1""").set_index("year").T

# %% [markdown]
# Vote result labels (checks the guess in `fct_vote_result.result_desc` accepted values).

# %%
q("""select result_code, result_desc, count(*) from marts.fct_vote_result
     group by 1, 2 order by 3 desc""")

# %%
q("""select status_id, status_desc, count(*) from marts.fct_bill
     group by 1, 2 order by 3 desc limit 15""")

# %% [markdown]
# ## Sample analyses

# %%
q("""select knesset_num, count(*) as bills, count(*) filter (where is_passed) as passed
     from marts.fct_bill group by 1 order by 1""").set_index("knesset_num").tail(10)

# %%
q(f"""select p.full_name, count(*) as bills_initiated
      from marts.fct_bill b cross join unnest(b.initiator_ids) as i(person_id)
      join marts.dim_person p using (person_id)
      where b.knesset_num = {KNESSET}
      group by 1 order by 2 desc limit 10""")

# %%
q(f"""select committee_name, count(*) as sessions
      from marts.fct_committee_session where knesset_num = {KNESSET}
      group by 1 order by 2 desc limit 10""")

# %%
q(f"""select ministry_name, count(*) as queries,
             round(avg((reply_minister_date is not null)::int) * 100) as pct_answered
      from marts.fct_query where knesset_num = {KNESSET}
      group by 1 order by 2 desc limit 10""")

# %% [markdown]
# Faction cohesion: share of a faction's for/against votes that match the faction's
# majority in that vote.

# %%
q(f"""with votes as (
          select r.vote_id, r.faction_name, r.result_desc
          from marts.fct_vote_result r
          join marts.fct_vote v using (vote_id)
          join marts.fct_plenum_session s on s.plenum_session_id = v.plenum_session_id
          where s.knesset_num = {KNESSET} and r.result_desc in ('בעד', 'נגד')
            and r.faction_name is not null
      ),
      majority as (
          select distinct on (vote_id, faction_name) vote_id, faction_name, result_desc
          from votes group by 1, 2, 3 order by vote_id, faction_name, count(*) desc
      )
      select v.faction_name, count(*) as mk_votes,
             round(avg((v.result_desc = m.result_desc)::int) * 100, 1) as pct_with_majority
      from votes v join majority m using (vote_id, faction_name)
      group by 1 order by 2 desc""")
