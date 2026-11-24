"""Researcher tools: what was said, by whom, where; and how much time each subject took."""

from dataclasses import fields
from typing import Any

from knesset.db import connect
from knesset.embed import plain
from knesset.search import Filters, Window, expand, search
from knesset.tools.common import rows, tool, truncate

FILTER_KEYS = {f.name for f in fields(Filters)}
FACETS = {  # by → (label expression, joins)
    "knesset": ("d.knesset_num::text", ""),
    "committee": (
        "coalesce(cm.committee_name, d.committee_id::text)",
        "left join marts.dim_committee cm on cm.committee_id = d.committee_id",
    ),
    "month": ("to_char(d.doc_date, 'YYYY-MM')", ""),
    "doc_type": ("d.group_type", ""),
    "speaker": (
        "coalesce(p.full_name, c.speaker_raw)",
        "left join marts.dim_person p using (person_id)",
    ),
    "faction": (
        "e.surface",
        "join text.chunk_entities e on e.chunk_id = c.chunk_id and e.source = 'speaker'"
        " and e.type = 'faction'",
    ),
    "entity": (
        "e.type || ': ' || e.normalized",
        "join text.chunk_entities e on e.chunk_id = c.chunk_id and e.source = 'ner'",
    ),
}


def to_filters(filters: dict[str, Any] | None) -> Filters:
    """Filters from an agent's JSON; unknown keys are an error the agent can read."""
    filters = dict(filters or {})
    unknown = set(filters) - FILTER_KEYS
    if unknown:
        raise ValueError(f"unknown filters {sorted(unknown)}; allowed: {sorted(FILTER_KEYS)}")
    if filters.get("items"):
        filters["items"] = [tuple(x) for x in filters["items"]]
    return Filters(**filters)


def shape(windows: list[Window]) -> dict:
    """Compact results: hits get more text than their neighbors."""
    results, evidence = [], []
    for w in windows:
        results.append(
            {
                "doc_id": w.doc_id,
                "doc_type": w.group_type or w.doc_type,
                "date": w.date,
                "knesset": w.knesset,
                "committee_id": w.committee_id,
                "url": w.url,
                "same_text_in": len(w.also_in),
                "chunks": [
                    {
                        "chunk_id": c.chunk_id,
                        "mark": c.mark,
                        "speaker": c.speaker,
                        "person_id": c.person_id,
                        "text": truncate(c.text, 800 if c.mark == "hit" else 300),
                    }
                    for c in w.chunks
                ],
            }
        )
        evidence += [
            {"type": "chunk", "ref": c.chunk_id, "url": w.url} for c in w.chunks if c.mark == "hit"
        ]
    return {"results": results, "evidence": evidence}


@tool
def search_text(
    query: str, filters: dict[str, Any] | None = None, k: int = 8, neighbors: int | None = None
) -> dict:
    """Hybrid (meaning + keyword) search over Knesset texts. Each hit comes with its neighbors
    (`before`/`after`; default 1 for protocols, 0 otherwise). Filters: knessets, committee_ids,
    date_from, date_to, doc_types, person_ids (speaker), faction_ids (speaker's faction),
    mentions (person ids mentioned), entity (a mentioned name), items ([item_type_id, item_id]),
    doc_ids."""
    return shape(search(query, to_filters(filters), min(k, 20), neighbors))


@tool
def get_document(doc_id: str, around_chunk: int | None = None, window: int = 3) -> dict:
    """A document's metadata and a stretch of its text: `window` chunks either side of
    `around_chunk`, or the first chunks."""
    with connect("agent_ro") as conn:
        meta = conn.execute(
            """select d.doc_id, d.parent_type, d.parent_id, d.group_type, d.knesset_num,
                      d.committee_id, d.doc_date::date::text, d.url, d.n_chars,
                      (select count(*) from text.chunks c where c.doc_id = d.doc_id)
               from text.documents d where d.doc_id = %s""",
            (doc_id,),
        ).fetchone()
        if meta is None:
            raise ValueError(f"no document {doc_id}")
        center = 0
        if around_chunk is not None:
            found = conn.execute(
                "select ord from text.chunks where chunk_id = %s and doc_id = %s",
                (around_chunk, doc_id),
            ).fetchone()
            center = found[0] if found else 0
        lo, hi = max(center - window, 0), center + window
        if around_chunk is None:
            hi = 2 * window
        chunks = conn.execute(
            """select chunk_id, ord, speaker_raw, person_id, text from text.chunks
               where doc_id = %s and ord between %s and %s order by ord""",
            (doc_id, lo, hi),
        ).fetchall()
    keys = ["doc_id", "parent_type", "parent_id", "doc_type", "knesset", "committee_id", "date",
            "url", "n_chars", "n_chunks"]  # fmt: skip
    doc = dict(zip(keys, meta, strict=True)) | {"url": meta[7].replace("\\", "/")}
    return {
        "document": doc,
        "chunks": [
            {
                "chunk_id": c,
                "ord": o,
                "speaker": s,
                "person_id": p,
                "text": truncate(plain(t), 1500),
            }
            for c, o, s, p, t in chunks
        ],
        "evidence": [{"type": "doc", "ref": doc_id, "url": doc["url"]}],
    }


@tool
def get_chunks(chunk_ids: list[int], neighbors: int = 0) -> dict:
    """Specific chunks (e.g. cited ones), with `neighbors` chunks either side."""
    with connect("agent_ro") as conn:
        windows = expand(conn, dict.fromkeys(chunk_ids[:30], 1.0), neighbors)
    return shape(windows)


@tool
def facets(by: str, filters: dict[str, Any] | None = None, query: str | None = None) -> dict:
    """Counts of matching chunks grouped `by`: knesset, committee, month, doc_type, speaker,
    faction or entity (top mentioned names). `query` narrows to chunks containing its words."""
    if by not in FACETS:
        raise ValueError(f"by must be one of {sorted(FACETS)}")
    label, join = FACETS[by]
    where, p = to_filters(filters).where()
    if query:
        where += " and c.tsv_stripped @@ websearch_to_tsquery('simple', text.he_strip(%(q)s))"
        p["q"] = query
    counts = rows(
        f"""select {label} as value, count(distinct c.chunk_id) as chunks,
                   count(distinct c.doc_id) as documents
            from text.chunks c join text.documents d using (doc_id) {join}
            where {where} group by 1 order by 2 desc limit 25""",
        p,
    )
    return {"by": by, "counts": counts, "evidence": []}


SHARE_BY = {  # by → (label over `b`, extra joins); `b` = the speaker rows in scope
    "section": ("coalesce(b.title, '(פתיחה)')", ""),
    "speaker": (
        "coalesce(pe.full_name, b.speaker_raw)",
        "left join marts.dim_person pe using (person_id)",
    ),
    "faction": (
        "coalesce(fac.faction_name, '(ללא סיעה)')",
        "left join fac using (person_id, doc_date)",
    ),
    "committee": (
        "coalesce(cm.committee_name, b.committee_id::text)",
        "left join marts.dim_committee cm using (committee_id)",
    ),
}
# The speaker's faction on the day: once per (person, date), with a hash join on person.
FACTIONS = """, fac as (
    select distinct on (x.person_id, x.doc_date) x.person_id, x.doc_date, pp.faction_name
    from (select distinct person_id, doc_date from b where person_id is not null) x
    join marts.fct_person_position pp on pp.person_id = x.person_id
         and pp.faction_id is not null and x.doc_date >= pp.start_date
         and x.doc_date < coalesce(pp.finish_date + interval '1 day', 'infinity')
    order by x.person_id, x.doc_date, pp.start_date desc)"""


@tool
def share_of_words(by: str, filters: dict[str, Any] | None = None, limit: int = 25) -> dict:
    """How the spoken words in protocols divide `by` section (agenda item), speaker, faction or
    committee: words, turns and percent of the total. Filters as in search_text (session-level
    ones apply: knessets, committee_ids, dates, doc_ids, person_ids, items)."""
    if by not in SHARE_BY:
        raise ValueError(f"by must be one of {sorted(SHARE_BY)}")
    f = to_filters(filters)
    label, join = SHARE_BY[by]
    cond, p = ["true"], {}
    if f.knessets:
        cond.append("d.knesset_num = any(%(k)s)")
        p["k"] = f.knessets
    if f.committee_ids:
        cond.append("d.committee_id = any(%(cm)s)")
        p["cm"] = f.committee_ids
    if f.date_from:
        cond.append("d.doc_date >= %(from)s")
        p["from"] = f.date_from
    if f.date_to:
        cond.append("d.doc_date < %(to)s::date + 1")
        p["to"] = f.date_to
    if f.doc_ids:
        cond.append("d.doc_id = any(%(docs)s)")
        p["docs"] = f.doc_ids
    if f.person_ids:
        cond.append("sp.person_id = any(%(p)s)")
        p["p"] = f.person_ids
    if f.items:
        cond.append(
            "(s.item_type_id, s.item_id) in"
            " (select * from unnest(%(it)s::int[], %(iid)s::bigint[]))"
        )
        p["it"], p["iid"] = [t for t, _ in f.items], [i for _, i in f.items]
    shares = rows(
        f"""with b as (
                select d.doc_id, d.doc_date, d.committee_id, s.title, sp.person_id,
                       sp.speaker_raw, sp.n_words, sp.n_turns
                from text.section_speakers sp
                join text.sections s using (doc_id, ord)
                join text.documents d using (doc_id)
                where {" and ".join(cond)}){FACTIONS if by == "faction" else ""}
            select {label} as value, sum(b.n_words) as words, sum(b.n_turns) as turns,
                   count(distinct b.doc_id) as sessions
            from b {join}
            group by 1 order by 2 desc""",
        p,
    )
    total = sum(r["words"] for r in shares) or 1
    for r in shares:
        r["pct"] = round(100 * r["words"] / total, 1)
    return {"by": by, "total_words": total, "shares": shares[:limit], "evidence": []}
