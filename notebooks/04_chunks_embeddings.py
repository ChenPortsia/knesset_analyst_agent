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
# # 04 · Chunks and embeddings
# After `knesset chunk` and `knesset embed`: chunk sizes, speaker resolution, keyword search
# with and without Hebrew prefix stripping, vector vs keyword vs hybrid search, neighbors.
# Run natively on the Mac with the embed extra (the vector part loads bge-m3):
# `uv run --extra embed jupytext --to ipynb --execute notebooks/04_chunks_embeddings.py`

# %% tags=["parameters"]
KNESSETS = [25, 26]
QUERIES = ["מכסות לייבוא דבש", "יוקר המחיה", "החטופים בעזה", "רפורמה משפטית"]
RUN_VECTOR = True
K = 5

# %%
import pandas as pd
from IPython.display import display

from knesset.nb import q, runs

scope = "{" + ",".join(map(str, KNESSETS)) + "}"
in_scope = f"d.knesset_num = any('{scope}')"

# %% [markdown]
# ## Chunks

# %%
q(f"""select meta->>'kind' as kind, count(*) as chunks, count(distinct c.doc_id) as docs,
             percentile_cont(array[0.1, 0.5, 0.9]) within group (order by n_tokens) as p10_50_90,
             max(n_tokens) as max_tokens,
             round(avg((embedding is not null)::int) * 100, 1) as pct_embedded
      from text.chunks c join text.documents d using (doc_id) where {in_scope}
      group by 1 order by 2 desc""")

# %%
q(f"""select d.priority, count(*) as chunks,
             round(avg((c.embedding is not null)::int) * 100, 2) as pct_embedded,
             count(distinct c.chunk_hash) as distinct_texts
      from text.chunks c join text.documents d using (doc_id) where {in_scope}
      group by 1 order by 1""")

# %%
p1 = q(f"""select count(*) filter (where embedding is null) as missing, count(*) as total
           from text.chunks c join text.documents d using (doc_id)
           where d.priority = 1 and {in_scope}""")
assert p1.total[0] > 0 and p1.missing[0] == 0, p1
p1

# %% [markdown]
# ## One protocol, chunk by chunk

# %%
doc = q(f"""select doc_id from text.documents d where priority = 1 and {in_scope}
            and chunked_hash is not null order by random() limit 1""").doc_id[0]
chunks = q(f"""select ord, meta->>'kind' kind, n_tokens, speaker_role, speaker_raw, person_id,
                      speaker_confidence, left(text, 160) as start
               from text.chunks where doc_id = '{doc}' order by ord""")
print(doc, len(chunks), "chunks")
chunks.head(12)

# %% [markdown]
# ## Speakers
# Protocol tags found (checks the markup the chunker expects).

# %%
q(f"""select speaker_role, count(*) as chunks,
             round(avg((person_id is not null)::int) * 100, 1) as pct_resolved
      from text.chunks c join text.documents d using (doc_id)
      where {in_scope} and speaker_raw is not null group by 1 order by 2 desc""")

# %% [markdown]
# Speaker match rate: share of speaker chunks resolved to `dim_person`, by confidence.
# Guests (אורח) are mostly invitees who are not in `dim_person`, so MK roles matter most.

# %%
q(f"""select coalesce(speaker_confidence::text, 'unresolved') as confidence, count(*),
             round(100.0 * count(*) / sum(count(*)) over (), 1) as pct
      from text.chunks c join text.documents d using (doc_id)
      where {in_scope} and speaker_raw is not null group by 1 order by 1""")

# %%
q(f"""select speaker_raw, speaker_role, count(*) from text.chunks c
      join text.documents d using (doc_id)
      where {in_scope} and speaker_raw is not null and person_id is null
        and speaker_role not in ('אורח')
      group by 1, 2 order by 3 desc limit 20""")

# %% [markdown]
# ## Keyword search: plain vs prefix-stripped

# %%
rows = []
for query in QUERIES:
    plain = q(
        f"""select count(*) from text.chunks c join text.documents d using (doc_id)
                  where {in_scope} and tsv @@ plainto_tsquery('simple', %s)""",
        (query,),
    )
    stripped = q(
        f"""select count(*) from text.chunks c join text.documents d using (doc_id)
                     where {in_scope}
                       and tsv_stripped @@ plainto_tsquery('simple', text.he_strip(%s))""",
        (query,),
    )
    rows.append((query, plain.iloc[0, 0], stripped.iloc[0, 0]))
pd.DataFrame(rows, columns=["query", "plain_hits", "stripped_hits"])

# %% [markdown]
# ## Vector vs keyword vs hybrid (reciprocal rank fusion)


# %%
def keyword(query: str, k: int = 50) -> pd.DataFrame:
    return q(
        f"""select c.chunk_id, c.doc_id, c.ord, left(c.text, 140) as start
                 from text.chunks c join text.documents d using (doc_id)
                 where {in_scope}
                   and c.tsv_stripped @@ plainto_tsquery('simple', text.he_strip(%(q)s))
                 order by ts_rank(c.tsv_stripped,
                                  plainto_tsquery('simple', text.he_strip(%(q)s))) desc
                 limit {k}""",
        {"q": query},
    )


def vector(query: str, k: int = 50) -> pd.DataFrame:
    vec = "[" + ",".join(map(str, encode([query])[0])) + "]"
    return q(
        f"""select c.chunk_id, c.doc_id, c.ord, left(c.text, 140) as start
                 from text.chunks c join text.documents d using (doc_id)
                 where {in_scope} and c.embedding is not null
                 order by c.embedding <=> %s::halfvec limit {k}""",
        (vec,),
    )


def hybrid(query: str, k: int = K) -> pd.DataFrame:
    scores: dict[int, float] = {}
    for df in (vector(query), keyword(query)):
        for rank, chunk_id in enumerate(df.chunk_id):
            scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
    top = sorted(scores, key=scores.get, reverse=True)[:k]
    return (
        q(
            """select chunk_id, doc_id, ord, left(text, 140) as start from text.chunks
                where chunk_id = any(%s)""",
            (top,),
        )
        .set_index("chunk_id")
        .loc[top]
        .reset_index()
    )


if RUN_VECTOR:
    from knesset.config import detect_device, settings
    from knesset.embed import load_encoder

    encode = load_encoder(detect_device(settings().device))
    for query in QUERIES:
        print(f"\n### {query}")
        for name, f in [("vector", vector), ("keyword", keyword), ("hybrid", hybrid)]:
            print(f"-- {name}")
            display(f(query).head(K)[["doc_id", "ord", "start"]])

# %% [markdown]
# ## Neighbor expansion
# The chunks before and after a hit (Phase 6 makes this a search option).

# %%
hit = keyword(QUERIES[0], 1)
if not hit.empty:
    doc_id, ord_ = hit.doc_id[0], int(hit.ord[0])
    display(
        q(
            """select ord, case when ord = %(o)s then 'hit' when ord < %(o)s then 'before'
                                  else 'after' end as role, speaker_raw, text
                 from text.chunks where doc_id = %(d)s and ord between %(o)s - 1 and %(o)s + 1
                 order by ord""",
            {"d": doc_id, "o": ord_},
        )
    )

# %% [markdown]
# ## Throughput

# %%
pd.concat([runs("chunk", 5), runs("embed", 5), runs("embed_index", 2)])[
    ["step", "started_at", "finished_at", "status", "rows_in", "rows_out", "throughput", "params"]
]
