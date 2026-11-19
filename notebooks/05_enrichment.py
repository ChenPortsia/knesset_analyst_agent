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
# # 05 · Enrichment
# Topics so far (teacher labels → classifier on the embeddings); NER, sentiment and the
# per-period clusters are added with their steps.

# %% tags=["parameters"]
KNESSETS = [25, 26]
SAMPLES = 8

# %%
import pandas as pd
from IPython.display import display

from knesset.config import settings
from knesset.enrich.llm import spent
from knesset.enrich.topic_clf import evaluate
from knesset.enrich.topics_llm import BUDGET_STEPS
from knesset.nb import q

scope = "{" + ",".join(map(str, KNESSETS)) + "}"

# %% [markdown]
# ## Budget (topics)

# %%
print(f"spent ${spent(BUDGET_STEPS):.2f} of ${settings().topic_budget_usd:.2f}")
q("""select step, batch_id, model, n_requests, projected_usd, cost_usd, status, created_at
     from ops.llm_batches order by created_at""")

# %% [markdown]
# ## Labels

# %%
q("""select source, count(*) as chunks, count(distinct version) as taxonomy_versions
     from text.topic_labels group by 1""")

# %% [markdown]
# ## Classifier quality
# Held-out LLM labels, and the hand-labeled gold set when present (`knesset label`).
# micro/macro F1 over topics; top1 = the first predicted topic is one of the gold topics.

# %%
pd.DataFrame(
    {
        m: {
            "threshold": r["threshold"],
            **{f"holdout_{k}": v for k, v in r["holdout"].items()},
            **{f"user_{k}": v for k, v in r.get("user", {}).items()},
        }
        for m, r in evaluate().items()
    }
).T

# %% [markdown]
# ## Topic distribution

# %%
topics = q(f"""select t.topic_id, p.name_he as parent, t.name_he as topic, count(*) as chunks
               from text.chunk_enrichment e
               join text.chunks c using (chunk_id) join text.documents d using (doc_id)
               cross join unnest(e.topics) as u(topic)
               join text.topics t on t.topic_id = u.topic::int
               join text.topics p on p.topic_id = t.parent_id
               where d.knesset_num = any('{scope}') group by 1, 2, 3 order by 4 desc""")
topics

# %% [markdown]
# ## Topics through time
# Share of chunks per parent topic and quarter (a chunk counts once per topic).

# %%
trend = q(f"""select date_trunc('quarter', d.doc_date)::date as quarter, p.name_he as parent,
                     count(*) as chunks
              from text.chunk_enrichment e
              join text.chunks c using (chunk_id) join text.documents d using (doc_id)
              cross join unnest(e.topics) as u(topic)
              join text.topics t on t.topic_id = u.topic::int
              join text.topics p on p.topic_id = t.parent_id
              where d.knesset_num = any('{scope}') and d.doc_date is not null
              group by 1, 2""")
if not trend.empty:
    share = trend.pivot(index="quarter", columns="parent", values="chunks").fillna(0)
    display((share.div(share.sum(axis=1), axis=0) * 100).round(1))

# %% [markdown]
# ## Samples

# %%
q(f"""select c.doc_id, e.topics, e.topic_scores, left(c.text, 220) as start
      from text.chunk_enrichment e join text.chunks c using (chunk_id)
      join text.documents d using (doc_id)
      where d.knesset_num = any('{scope}') and e.topics is not null
      order by random() limit {SAMPLES}""")
