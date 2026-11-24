"""Hybrid retrieval over text.chunks: vector top-50 + keyword top-50, merged by reciprocal rank
fusion, then each hit expanded with its neighbors (ord ± n) into merged windows.

The query is embedded with bge-m3, as the chunks were. Where torch is not installed (the app
container) or no encoder is given, search falls back to keywords only.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import structlog

from knesset.db import connect
from knesset.embed import plain

CANDIDATES = 50  # per method, before fusion
EXACT_MAX = 20_000  # filtered chunks compared exactly; above, the index's TOP_N, filtered
TOP_N = 1000  # = hnsw.ef_search's maximum
RRF_K = 60
SESSION_TYPES = ("committee_session", "plenum_session")

Encode = Callable[[list[str]], Sequence[Sequence[float]]]
log = structlog.get_logger()
_encoder: Encode | None = None


@dataclass
class Filters:
    """All optional; lists match any. Ids come from resolve_entity."""

    knessets: list[int] | None = None
    committee_ids: list[int] | None = None
    date_from: date | None = None
    date_to: date | None = None
    doc_types: list[str] | None = None  # parent_type: committee_session, plenum_session, bill, …
    person_ids: list[int] | None = None  # who spoke
    faction_ids: list[int] | None = None  # the speaker's faction on the day
    mentions: list[int] | None = None  # people mentioned (NER, linked to dim_person)
    entity: str | None = None  # any named entity, by normalized name (NER)
    items: list[tuple[int, int]] | None = None  # (item_type_id, item_id): bill, query, …
    doc_ids: list[str] | None = None
    include_headers: bool = False  # attendance lists and tables of contents

    @property
    def selective(self) -> bool:
        """Filters that leave a small share of the chunks (not Knesset, type or dates)."""
        return any(
            (self.committee_ids, self.person_ids, self.faction_ids, self.mentions, self.entity,
             self.items, self.doc_ids)
        )  # fmt: skip

    def where(self) -> tuple[str, dict[str, Any]]:
        """SQL conditions on chunks `c` and documents `d`, and their parameters."""
        sql, p = ["true"], {}

        def add(cond: str, **params: Any) -> None:
            sql.append(cond)
            p.update(params)

        if not self.include_headers:
            add("c.meta->>'kind' <> 'header'")
        if self.knessets:
            add("d.knesset_num = any(%(f_k)s)", f_k=self.knessets)
        if self.committee_ids:
            add("d.committee_id = any(%(f_cm)s)", f_cm=self.committee_ids)
        if self.date_from:
            add("d.doc_date >= %(f_from)s", f_from=self.date_from)
        if self.date_to:
            add("d.doc_date < %(f_to)s::date + 1", f_to=self.date_to)
        if self.doc_types:
            add("d.parent_type = any(%(f_dt)s)", f_dt=self.doc_types)
        if self.doc_ids:
            add("c.doc_id = any(%(f_doc)s)", f_doc=self.doc_ids)
        if self.person_ids:
            add("c.person_id = any(%(f_p)s)", f_p=self.person_ids)
        if self.faction_ids:
            add(
                """exists (select 1 from text.chunk_entities e where e.chunk_id = c.chunk_id
                   and e.source = 'speaker' and e.type = 'faction' and e.ref_id = any(%(f_f)s))""",
                f_f=self.faction_ids,
            )
        if self.mentions:
            add(
                """exists (select 1 from text.chunk_entities e where e.chunk_id = c.chunk_id
                   and e.source = 'ner' and e.type = 'person' and e.ref_id = any(%(f_m)s))""",
                f_m=self.mentions,
            )
        if self.entity:
            add(
                """exists (select 1 from text.chunk_entities e where e.chunk_id = c.chunk_id
                   and e.source = 'ner' and e.normalized = %(f_e)s)""",
                f_e=self.entity,
            )
        if self.items:
            add(
                """exists (select 1 from text.chunk_items i where i.chunk_id = c.chunk_id
                   and (i.item_type_id, i.item_id) in (select * from unnest(%(f_it)s::int[],
                                                                         %(f_iid)s::bigint[])))""",
                f_it=[t for t, _ in self.items],
                f_iid=[i for _, i in self.items],
            )
        return " and ".join(sql), p


@dataclass
class Chunk:
    chunk_id: int
    ord: int
    mark: str  # hit, before or after
    speaker: str | None
    person_id: int | None
    text: str


@dataclass
class Window:
    """Consecutive chunks of one document around one or more hits. Chunk text is shown without
    protocol markup (`<< דובר >> X: << דובר >>` → `X:`)."""

    doc_id: str
    url: str
    doc_type: str
    group_type: str | None
    knesset: int | None
    committee_id: int | None
    date: str | None
    score: float  # best fused score of its hits
    chunks: list[Chunk] = field(default_factory=list)
    also_in: list[str] = field(default_factory=list)  # other documents with the same hit text


def default_encoder() -> Encode | None:
    """bge-m3 on the local device, loaded once; None where the `embed` extra is missing."""
    global _encoder
    if _encoder is None:
        try:
            from knesset.config import detect_device, settings
            from knesset.embed import load_encoder

            _encoder = load_encoder(detect_device(settings().device))
        except ImportError:
            log.warning("search_keywords_only", reason="sentence-transformers not installed")
            return None
    return _encoder


def vector_ids(
    conn: Any, vec: Sequence[float], where: str, p: dict, selective: bool
) -> tuple[list[int], str]:
    """Nearest chunks by cosine distance. Without a selective filter: the HNSW index with the
    filter applied as it scans. With one (a committee, a speaker, …), the index would discard
    most neighbors and keep scanning (2 s for one committee), so:
    - up to EXACT_MAX matching chunks: exact distances over them (≈ 80 ms per 20k);
    - more: the index's TOP_N nearest overall, then the filter (a big committee keeps plenty).
    """
    p = p | {"v": "[" + ",".join(f"{x:.5f}" for x in vec) + "]"}
    scope = f"""from text.chunks c join text.documents d using (doc_id)
                where c.embedding is not null and {where}"""
    if selective:
        (n,) = conn.execute(
            f"select count(*) from (select 1 {scope} limit {EXACT_MAX + 1}) t", p
        ).fetchone()
        path = "exact" if n <= EXACT_MAX else "top_n"
        if path == "exact":
            sql = f"""with cand as materialized (select c.chunk_id, c.embedding {scope})
                      select chunk_id from cand order by embedding <=> %(v)s::halfvec
                      limit {CANDIDATES}"""
        else:
            conn.execute(f"set local hnsw.ef_search = {TOP_N}")
            sql = f"""with top as materialized (
                          select chunk_id, embedding <=> %(v)s::halfvec as dist
                          from text.chunks where embedding is not null
                          order by embedding <=> %(v)s::halfvec limit {TOP_N})
                      select c.chunk_id from top join text.chunks c using (chunk_id)
                      join text.documents d using (doc_id)
                      where {where} order by top.dist limit {CANDIDATES}"""
    else:
        path = "index"
        conn.execute("set local hnsw.ef_search = 100")
        conn.execute("select set_config('hnsw.iterative_scan', 'relaxed_order', true)")
        sql = (
            f"select c.chunk_id {scope} order by c.embedding <=> %(v)s::halfvec limit {CANDIDATES}"
        )
    return [r[0] for r in conn.execute(sql, p).fetchall()], path


def keyword_ids(conn: Any, query: str, where: str, p: dict) -> list[int]:
    rows = conn.execute(
        f"""select c.chunk_id from text.chunks c join text.documents d using (doc_id),
                   websearch_to_tsquery('simple', text.he_strip(%(q)s)) as q
            where c.tsv_stripped @@ q and {where}
            order by ts_rank_cd(c.tsv_stripped, q) desc, c.chunk_id limit {CANDIDATES}""",
        p | {"q": query},
    ).fetchall()
    return [r[0] for r in rows]


def fuse(*rankings: list[int]) -> dict[int, float]:
    """Reciprocal rank fusion: each list adds 1 / (60 + rank)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (RRF_K + rank)
    return scores


def dedupe(conn: Any, scores: dict[int, float], k: int) -> tuple[dict[int, float], dict]:
    """The k best chunks with distinct text (the same background document is attached to many
    sessions), and for each kept chunk the other documents its text appears in."""
    rows = conn.execute(
        "select chunk_id, chunk_hash, doc_id from text.chunks where chunk_id = any(%s)",
        (list(scores),),
    ).fetchall()
    info = {cid: (h, doc) for cid, h, doc in rows}
    kept: dict[str, int] = {}
    also: dict[int, list[str]] = {}
    for cid in sorted(scores, key=lambda x: -scores[x]):
        h, doc = info[cid]
        if h in kept:
            also[kept[h]].append(doc)
        elif len(kept) < k:
            kept[h] = cid
            also[cid] = []
    return {cid: scores[cid] for cid in kept.values()}, also


def expand(conn: Any, hits: dict[int, float], neighbors: int | None) -> list[Window]:
    """Fetch hits and their neighbors in one query; merge overlapping windows per document.
    neighbors=None means 1 for protocols and 0 for other documents."""
    if not hits:
        return []
    rows = conn.execute(
        """with h as (
               select c.doc_id, c.ord,
                      coalesce(%(n)s, case when d.parent_type = any(%(sessions)s) then 1 else 0
                                      end) as n
               from text.chunks c join text.documents d using (doc_id)
               where c.chunk_id = any(%(ids)s))
           select distinct c.chunk_id, c.doc_id, c.ord, c.speaker_raw, c.person_id, c.text,
                  d.url, d.parent_type, d.group_type, d.knesset_num, d.committee_id,
                  d.doc_date::date::text
           from h join text.chunks c on c.doc_id = h.doc_id and c.ord between h.ord - h.n
                                                                         and h.ord + h.n
           join text.documents d on d.doc_id = c.doc_id
           order by c.doc_id, c.ord""",
        {"ids": list(hits), "n": neighbors, "sessions": list(SESSION_TYPES)},
    ).fetchall()
    windows: list[Window] = []
    for cid, doc_id, ord_, speaker, pid, text, url, ptype, gtype, knesset, committee, day in rows:
        w = windows[-1] if windows else None
        if not w or w.doc_id != doc_id or w.chunks[-1].ord + 1 < ord_:
            w = Window(doc_id, url.replace("\\", "/"), ptype, gtype, knesset, committee, day, 0)
            windows.append(w)
        w.chunks.append(Chunk(cid, ord_, "hit" if cid in hits else "", speaker, pid, plain(text)))
        w.score = max(w.score, hits.get(cid, 0.0))
    for w in windows:
        first = next(c.ord for c in w.chunks if c.mark == "hit")
        for c in w.chunks:
            c.mark = c.mark or ("before" if c.ord < first else "after")
    return sorted(windows, key=lambda w: -w.score)


def search(
    query: str,
    filters: Filters | None = None,
    k: int = 10,
    neighbors: int | None = None,
    encode: Encode | None | bool = True,
) -> list[Window]:
    """The k best chunks for a query, as windows with their neighbors, best first.
    encode=True loads bge-m3; False or None searches keywords only."""
    filters = filters or Filters()
    where, p = filters.where()
    started = time.perf_counter()
    encoder = default_encoder() if encode is True else encode or None
    vec = encoder([query])[0] if encoder else None
    with connect("agent_ro") as conn, conn.transaction():
        by_vector, path = (
            vector_ids(conn, vec, where, p, filters.selective) if vec is not None else ([], "none")
        )
        by_keyword = keyword_ids(conn, query, where, p)
        top, also = dedupe(conn, fuse(by_vector, by_keyword), k)
        windows = expand(conn, top, neighbors)
    for w in windows:
        w.also_in = sorted({d for ch in w.chunks for d in also.get(ch.chunk_id, [])} - {w.doc_id})
    log.info(
        "search",
        ms=round((time.perf_counter() - started) * 1000),
        vector=len(by_vector),
        vector_path=path,
        keyword=len(by_keyword),
        hits=len(top),
        windows=len(windows),
    )
    return windows
