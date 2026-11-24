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
# Structure from the documents' own markup, and named entities:
# 1. links: chunks → session items, speakers → person and faction (`knesset links`);
# 2. protocol sections and attendance lists (`knesset structure`);
# 3. named entities (`make ner`).
#
# Topic labeling (taxonomy, LLM teacher, classifier) is paused: the agent answers topic
# questions from section titles and word counts instead.

# %% tags=["parameters"]
KNESSETS = [25, 26]
SAMPLES = 10

# %%
from knesset.nb import q

scope = {"k": KNESSETS}
IN_SCOPE = "d.knesset_num = any(%(k)s)"

# %% [markdown]
# ## 1 · Links
# Share of chunks linked to a session item, and with a resolved speaker.

# %%
q(
    f"""select d.parent_type, count(*) as chunks,
           round(100.0 * count(*) filter (where exists (
               select 1 from text.chunk_items i where i.chunk_id = c.chunk_id)) / count(*), 1)
               as pct_with_item,
           round(100.0 * count(c.person_id) / nullif(count(c.speaker_raw), 0), 1)
               as pct_speakers_resolved
        from text.chunks c join text.documents d using (doc_id)
        where {IN_SCOPE} and d.parent_type in ('committee_session', 'plenum_session')
        group by 1""",
    scope,
)

# %% [markdown]
# ## 2 · Sections
# Each protocol split at its agenda markers; words and turns per section and speaker.

# %%
q(
    f"""select d.parent_type, count(*) as sections, count(distinct s.doc_id) as protocols,
           round(100.0 * count(s.title) / count(*), 1) as pct_titled,
           round(100.0 * count(s.item_id) / count(*), 1) as pct_linked_to_item,
           percentile_cont(0.5) within group (order by s.n_words) as median_words
        from text.sections s join text.documents d using (doc_id)
        where {IN_SCOPE} group by 1""",
    scope,
)

# %% [markdown]
# One committee session with several agenda items: the share of words per item, and who
# spoke in the largest one.

# %%
session = q(
    f"""select s.doc_id from text.sections s join text.documents d using (doc_id)
        where {IN_SCOPE} and d.parent_type = 'committee_session'
        group by 1 having count(*) >= 3 order by random() limit 1""",
    scope,
)
doc_id = session.doc_id[0] if len(session) else None
q(
    """select ord, left(title, 80) as title, n_words, n_turns,
              round(100.0 * n_words / sum(n_words) over (), 1) as pct_of_session
       from text.sections where doc_id = %(d)s order by ord""",
    {"d": doc_id},
)

# %%
q(
    """select sp.speaker_raw, sp.role, sp.person_id is not null as resolved,
              sp.n_words, sp.n_turns
       from text.section_speakers sp
       where sp.doc_id = %(d)s and sp.ord = (
           select ord from text.sections where doc_id = %(d)s order by n_words desc limit 1)
       order by sp.n_words desc""",
    {"d": doc_id},
)

# %% [markdown]
# ## 3 · Participants
# Committee attendance lists: who was invited, under which heading, from which organization.

# %%
q(
    f"""select p.grp, count(*) as participants, count(p.person_id) as linked_to_person,
           count(p.organization) as with_organization
        from text.participants p join text.documents d using (doc_id)
        where {IN_SCOPE} group by 1 order by 2 desc limit 12""",
    scope,
)

# %%
q(
    f"""select p.organization, count(*) as appearances, count(distinct p.doc_id) as sessions
        from text.participants p join text.documents d using (doc_id)
        where {IN_SCOPE} and p.organization is not null
        group by 1 order by 2 desc limit 20""",
    scope,
)

# %% [markdown]
# ## 4 · Named entities
# DictaBERT-NER over chunk text (speaker lines and markup removed). People link to
# `dim_person` when the name, or a last name alone, belongs to one person holding a position
# at that date; parties link to their faction.

# %%
q(
    f"""select e.type, count(*) as entities, count(distinct e.normalized) as distinct_names,
           round(100.0 * count(e.ref_id) / count(*), 1) as pct_linked
        from text.chunk_entities e join text.chunks c using (chunk_id)
        join text.documents d using (doc_id)
        where e.source = 'ner' and {IN_SCOPE} group by 1 order by 2 desc""",
    scope,
)

# %%
q(
    f"""select type, normalized, n from (
            select e.type, e.normalized, count(*) as n,
                   row_number() over (partition by e.type order by count(*) desc) as rank
            from text.chunk_entities e join text.chunks c using (chunk_id)
            join text.documents d using (doc_id)
            where e.source = 'ner' and e.type in ('person', 'org', 'place', 'misc')
              and {IN_SCOPE}
            group by 1, 2) t
        where rank <= 8 order by type, n desc""",
    scope,
)

# %% [markdown]
# Spot check: last-name-only and prefixed mentions that were linked to a person.

# %%
q(
    f"""select e.surface, e.normalized, count(*) as n
        from text.chunk_entities e join text.chunks c using (chunk_id)
        join text.documents d using (doc_id)
        where e.source = 'ner' and e.type = 'person' and e.ref_id is not null
          and e.surface <> e.normalized and {IN_SCOPE}
        group by 1, 2 order by random() limit %(n)s""",
    scope | {"n": SAMPLES},
)
