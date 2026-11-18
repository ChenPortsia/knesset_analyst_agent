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
# # 03 · Documents
# The Word-text registry (`text.documents`) after `knesset registry` and `knesset extract`:
# status by Knesset and type, the PDF-only gap, failures, text quality, and a random reader.

# %% tags=["parameters"]
KNESSETS = [25, 26]
SAMPLE = 20
MIN_DONE_PCT = 98.0  # acceptance: priority-1 Word documents extracted

# %%
from knesset.nb import failures, q

scope = "{" + ",".join(map(str, KNESSETS)) + "}"

# %% [markdown]
# ## Status

# %%
q("select status, count(*) from text.documents group by 1 order by 2 desc")

# %%
q(f"""select priority, status, count(*) from text.documents
      where knesset_num = any('{scope}') group by 1, 2 order by 1, 3 desc""")

# %%
q("""select parent_type, group_type, file_type, status, count(*)
     from text.documents group by 1, 2, 3, 4 order by 5 desc limit 30""")

# %% [markdown]
# Priority 1 (committee protocols in the scope Knessets): share done among Word documents.

# %%
p1 = q(f"""select count(*) filter (where status = 'done') as done,
                  count(*) filter (where status in ('done', 'pending', 'failed')) as word
           from text.documents where priority = 1 and knesset_num = any('{scope}')""")
p1["pct_done"] = (100 * p1.done / p1.word.clip(lower=1)).round(2)
p1

# %%
assert p1.pct_done[0] >= MIN_DONE_PCT, p1

# %% [markdown]
# ## PDF-only gap
# Parents (session, bill, …) and document types available only as PDF, so without text.

# %%
q("""select parent_type, knesset_num, group_type, count(*) as pdf_only_files,
            count(distinct parent_id) as parents
     from text.documents where status = 'skipped_pdf'
     group by 1, 2, 3 order by 1, 2 desc nulls last, 4 desc""")

# %%
q("""with by_type as (
         select parent_type, group_type,
                count(distinct parent_id) filter (where status = 'skipped_pdf') as pdf_only,
                count(distinct parent_id) filter (where status in ('done', 'pending', 'failed'))
                    as with_word
         from text.documents group by 1, 2
     )
     select *, round(100.0 * pdf_only / nullif(pdf_only + with_word, 0), 1) as pct_pdf_only
     from by_type where pdf_only > 0 order by pdf_only desc limit 20""")

# %% [markdown]
# ## Failures

# %%
q("""select left(error, 80) as error, count(*) from text.documents
     where status = 'failed' or (status = 'pending' and attempts > 0) group by 1 order by 2 desc""")

# %%
failures("extract", n=10)

# %% [markdown]
# ## Text quality

# %%
q("""select detected_type, count(*) as docs,
            percentile_cont(0.5) within group (order by n_chars)::int as median_chars,
            min(n_chars) as min_chars, max(n_chars) as max_chars,
            count(*) filter (where n_chars < 500) as under_500_chars,
            count(*) filter (where text like '%' || chr(65533) || '%') as with_broken_chars
     from text.documents where status = 'done' group by 1""")

# %% [markdown]
# Share of Hebrew letters among letters: low values suggest a broken encoding.

# %%
q("""select round(heb::numeric / nullif(heb + lat, 0), 1) as hebrew_share, count(*)
     from (select length(regexp_replace(text, '[^א-ת]', '', 'g')) as heb,
                  length(regexp_replace(text, '[^A-Za-z]', '', 'g')) as lat
           from text.documents where status = 'done') t
     group by 1 order by 1""")

# %% [markdown]
# ## Random reader
# Read these and check that the Hebrew, speaker lines and paragraphs come out right.

# %%
sample = q(f"""select doc_id, parent_type, knesset_num, group_type, detected_type, n_chars, url,
                      left(text, 600) as start
               from text.documents
               where status = 'done' and priority = 1 and knesset_num = any('{scope}')
               order by random() limit {SAMPLE}""")
for r in sample.itertuples():
    print(
        f"=== {r.doc_id} · K{r.knesset_num} · {r.group_type} · {r.detected_type}"
        f" · {r.n_chars:,} chars\n{r.url}\n{r.start}\n"
    )
