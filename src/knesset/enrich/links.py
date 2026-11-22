"""Tier 1 enrichment: structured links that need no model.

- chunk_items: protocol chunks → the session items (bills, queries, motions, …) they discuss.
  Protocols mark agenda topics with `<< נושא >> title << נושא >>`; walking a protocol's chunks
  in order, the current topic is matched by name to the session's items. Header chunks link to
  every item their agenda lists; a one-item session links all its chunks to that item.
- chunk_entities (source 'speaker'): each resolved speaker as a person, and their faction on
  the session date.
"""

import re
from typing import Any

import structlog

from knesset import log as logs
from knesset.db import connect, migrate

TOPIC = r"<<\s*נושא\s*>>([^<]*)<<\s*נושא\s*>>"
MIN_OVERLAP = 0.6  # shared words / words of the shorter title
SESSION_TYPES = {"committee_session": "committee", "plenum_session": "plenum"}

log = structlog.get_logger()


def words(title: str) -> set[str]:
    """Content words of a title: no numbering, punctuation or one-letter words."""
    title = re.sub(r"[^\w\s]", " ", title.replace("״", '"'))
    return {w for w in title.split() if len(w) > 1 and not w.isdigit()}


def best_item(topic: str, items: list[tuple[int, int, set[str]]]) -> tuple[int, int] | None:
    """(item_type_id, item_id) of the session item whose name best matches a topic title."""
    t = words(topic)
    best, best_score = None, 0.0
    for type_id, item_id, name in items:
        if t and name:
            score = len(t & name) / min(len(t), len(name))
            if score > best_score:
                best, best_score = (type_id, item_id), score
    return best if best_score >= MIN_OVERLAP else None


def link_chunks(
    chunks: list[tuple[int, str, list[str]]], items: list[tuple[int, int, set[str]]]
) -> list[tuple[int, int, int]]:
    """(chunk_id, item_type_id, item_id) rows for one protocol's chunks, given in order as
    (chunk_id, kind, topic titles in the chunk). A chunk where a topic starts belongs to the
    topic before it and to the new one; a topic that matches no item ends the previous one."""
    only = (items[0][0], items[0][1]) if len(items) == 1 else None
    rows: set[tuple[int, int, int]] = set()
    current: tuple[int, int] | None = None
    for chunk_id, kind, topics in chunks:
        matched = [best_item(t, items) for t in topics]
        rows |= {(chunk_id, *m) for m in matched if m}
        if kind != "header":
            if link := current or only:
                rows.add((chunk_id, *link))
            if topics:
                current = matched[-1]
        elif only:
            rows.add((chunk_id, *only))
    return sorted(rows)


def session_items(conn: Any) -> dict[tuple[str, int], list[tuple[int, int, set[str]]]]:
    """(session type, session id) → [(item_type_id, item_id, title words)]."""
    items: dict[tuple[str, int], list[tuple[int, int, set[str]]]] = {}
    for stype, sid, type_id, item_id, name in conn.execute(
        "select session_type, session_id, item_type_id, item_id, item_name"
        " from marts.fct_session_item where item_id is not null"
    ):
        items.setdefault((stype, sid), []).append((type_id, item_id, words(name or "")))
    return items


def link_items(knessets: list[int] | None) -> int:
    """Rebuild chunk_items for protocol chunks in the given Knessets. Returns rows written."""
    params = {"k": knessets}
    scope = "(%(k)s::int[] is null or d.knesset_num = any(%(k)s))"
    with connect() as conn:
        items = session_items(conn)
        rows: list[tuple[int, int, int]] = []
        doc_chunks: list[tuple[int, str, list[str]]] = []
        key = None
        cur = conn.execute(
            f"""select d.parent_type, d.parent_id, c.doc_id, c.chunk_id, c.meta->>'kind',
                       array(select m[1] from regexp_matches(c.text, %(topic)s, 'g') as m)
                from text.chunks c join text.documents d using (doc_id)
                where d.parent_type in ('committee_session', 'plenum_session') and {scope}
                order by c.doc_id, c.ord""",
            params | {"topic": TOPIC},
        )
        for ptype, pid, doc_id, chunk_id, kind, topics in cur:
            if key and key[0] != doc_id:
                rows += link_chunks(doc_chunks, items.get(key[1], []))
                doc_chunks = []
            key = (doc_id, (SESSION_TYPES[ptype], pid))
            doc_chunks.append((chunk_id, kind, [t.strip() for t in topics]))
        if key:
            rows += link_chunks(doc_chunks, items.get(key[1], []))

        conn.execute(
            f"""delete from text.chunk_items ci using text.chunks c, text.documents d
                where ci.chunk_id = c.chunk_id and c.doc_id = d.doc_id and {scope}""",
            params,
        )
        with conn.cursor().copy(
            "copy text.chunk_items (chunk_id, item_type_id, item_id) from stdin"
        ) as copy:
            for row in rows:
                copy.write_row(row)
    return len(rows)


SPEAKER_SQL = """
insert into text.chunk_entities
    (chunk_id, type, surface, normalized, ref_table, ref_id, confidence, source)
select c.chunk_id, 'person', c.speaker_raw, p.full_name, 'dim_person', c.person_id,
       c.speaker_confidence, 'speaker'
from text.chunks c
join text.documents d using (doc_id)
join marts.dim_person p using (person_id)
where c.person_id is not null and {scope};

insert into text.chunk_entities
    (chunk_id, type, surface, normalized, ref_table, ref_id, confidence, source)
select distinct on (c.chunk_id) c.chunk_id, 'faction', pp.faction_name, pp.faction_name,
       'dim_faction', pp.faction_id, c.speaker_confidence, 'speaker'
from text.chunks c
join text.documents d using (doc_id)
join marts.fct_person_position pp on pp.person_id = c.person_id and pp.faction_id is not null
     and d.doc_date >= pp.start_date
     and d.doc_date < coalesce(pp.finish_date + interval '1 day', 'infinity')
where c.person_id is not null and {scope}
order by c.chunk_id, pp.start_date desc;
"""


def link_speakers(knessets: list[int] | None) -> dict[str, int]:
    """Rebuild the speaker person/faction entities. Returns entity counts."""
    params = {"k": knessets}
    scope = "(%(k)s::int[] is null or d.knesset_num = any(%(k)s))"
    with connect() as conn:
        conn.execute(
            f"""delete from text.chunk_entities e using text.chunks c, text.documents d
                where e.chunk_id = c.chunk_id and c.doc_id = d.doc_id
                  and e.source = 'speaker' and {scope}""",
            params,
        )
        for statement in SPEAKER_SQL.format(scope=scope).split(";"):
            if statement.strip():
                conn.execute(statement, params)
        counts = dict(
            conn.execute(
                f"""select e.type, count(*) from text.chunk_entities e
                    join text.chunks c using (chunk_id) join text.documents d using (doc_id)
                    where e.source = 'speaker' and {scope} group by 1""",
                params,
            ).fetchall()
        )
    return {"person": counts.get("person", 0), "faction": counts.get("faction", 0)}


def link(knessets: list[int] | None) -> dict[str, int]:
    """Tier 1: chunk_items and speaker entities."""
    migrate()
    with logs.run("links", knessets=knessets) as run:
        totals = {"chunk_items": link_items(knessets), **link_speakers(knessets)}
        run.rows_out = sum(totals.values())
        log.info("links_done", **totals)
    return totals
