"""Summarizer tools: a document's structure at a glance, and map-reduce summaries."""

from collections.abc import Callable
from typing import Any

from knesset.tools.common import rows, tool, truncate
from knesset.tools.research import search_text

LLM = Callable[[str], str]  # prompt → text; the agent's model, given in Phase 7


@tool
def get_document_summary(doc_id: str) -> dict:
    """A protocol or document at a glance, from its markup (no model): agenda sections with
    their share of the words, top speakers, invited organizations and most-mentioned names."""
    meta = rows(
        """select doc_id, group_type as doc_type, knesset_num as knesset, committee_id,
                  doc_date::date as date, replace(url, '\\', '/') as url
           from text.documents where doc_id = %(d)s""",
        {"d": doc_id},
    )
    if not meta:
        raise ValueError(f"no document {doc_id}")
    p = {"d": doc_id}
    sections = rows(
        """select ord, title, item_type_id, item_id, n_words, n_turns,
                  round(100.0 * n_words / nullif(sum(n_words) over (), 0), 1) as pct
           from text.sections where doc_id = %(d)s order by ord""",
        p,
    )
    speakers = rows(
        """select speaker_raw as speaker, person_id, sum(n_words) as words, sum(n_turns) as turns
           from text.section_speakers where doc_id = %(d)s
           group by 1, 2 order by 3 desc limit 10""",
        p,
    )
    organizations = rows(
        """select organization, count(*) as people from text.participants
           where doc_id = %(d)s and organization is not null group by 1 order by 2 desc limit 10""",
        p,
    )
    entities = rows(
        """select e.type, e.normalized as name, count(*) as mentions
           from text.chunk_entities e join text.chunks c using (chunk_id)
           where c.doc_id = %(d)s and e.source = 'ner' and e.type in ('person', 'org', 'place')
           group by 1, 2 order by 3 desc limit 15""",
        p,
    )
    for s in sections:
        s["title"] = truncate(s["title"], 200)
    return {
        "document": meta[0],
        "sections": sections,
        "top_speakers": speakers,
        "organizations": organizations,
        "top_entities": entities,
        "evidence": [{"type": "doc", "ref": doc_id, "url": meta[0]["url"]}],
    }


@tool
def summarize(
    question: str, llm: LLM, filters: dict[str, Any] | None = None, max_items: int = 12
) -> dict:
    """Map-reduce over the best-matching passages: one note per passage, then one summary of
    the notes. The notes keep their chunk ids, so the summary can be cited."""
    found = search_text(question, filters, k=max_items)
    notes = []
    for r in found["results"]:
        passage = "\n".join(c["text"] or "" for c in r["chunks"])
        note = llm(
            f"Question: {question}\nSource: {r['doc_type']} {r['date']}\n\n{passage}\n\n"
            "In 1-3 sentences, what does this passage say that bears on the question? "
            "Answer NONE if nothing."
        ).strip()
        if note and note.upper() != "NONE":
            hits = [c["chunk_id"] for c in r["chunks"] if c["mark"] == "hit"]
            notes.append({"chunk_ids": hits, "date": r["date"], "note": note})
    listing = "\n".join(f"- [{n['chunk_ids']}] {n['date']}: {n['note']}" for n in notes)
    summary = llm(
        f"Question: {question}\n\nNotes from the sources (chunk ids in brackets):\n{listing}\n\n"
        "Write a short answer from these notes only, citing chunk ids in brackets."
    ) if notes else "No relevant passages found."  # fmt: skip
    return {"summary": summary, "notes": notes, "evidence": found["evidence"]}
