"""Named entities in chunk text with a Hebrew NER model (DictaBERT-NER), linked to the marts
where the name is unambiguous: people → dim_person, parties → their faction id.

Runs natively where the GPU is (`make ner`). Speaker lines and other protocol markup are
removed first: speakers are already entities (source 'speaker'), and attendance lists are in
text.participants, so header chunks are skipped. Resumable: text.chunk_ner records the chunks
read; a re-chunked document is read again.
"""

import re
from collections.abc import Callable
from typing import Any

import structlog

from knesset import log as logs
from knesset.chunks import People
from knesset.config import detect_device, settings
from knesset.db import connect, migrate
from knesset.enrich.structure import MARKUP

MODEL = "dicta-il/dictabert-ner"
BATCH = 512  # chunks per database round trip
TYPES = {  # model label → chunk_entities.type; dates (TIMEX) and titles (TTL) are skipped
    "PER": "person",
    "ORG": "org",
    "GPE": "place",
    "LOC": "place",
    "FAC": "facility",
    "EVE": "event",
    "DUC": "product",
    "WOA": "work",
    "ANG": "language",
    "MISC": "misc",
}
PREFIXES = "בהוכלמש"  # one-letter Hebrew prefixes: במשרד → משרד, והליכוד → ליכוד → הליכוד
MIN_SCORE = 0.5
ALIASES = {"ביבי": "בנימין נתניהו"}

# texts → per text: [{"entity_group", "score", "start", "end"}]
Tagger = Callable[[list[str]], list[list[dict[str, Any]]]]
log = structlog.get_logger()


def load_tagger(device: str) -> Tagger:
    from transformers import pipeline  # the `embed` extra

    nlp = pipeline("ner", model=MODEL, aggregation_strategy="first", device=device, stride=64)

    def tag(texts: list[str]) -> list[list[dict[str, Any]]]:
        keep = [i for i, t in enumerate(texts) if t.strip()]  # the model fails on empty text
        found = nlp([texts[i] for i in keep], batch_size=32) if keep else []
        out: list[list[dict[str, Any]]] = [[] for _ in texts]
        for i, f in zip(keep, found, strict=True):
            out[i] = f
        return out

    return tag


def ner_text(text: str) -> str:
    """Chunk text without speaker lines and markup."""
    return re.sub(r"\n{2,}", "\n", MARKUP.sub("", text)).strip()


def candidates(surface: str) -> list[str]:
    """The name, and the name without one or two prefix letters. The article ה stays: after
    ב/ל/כ it is implied, so בכנסת → הכנסת first, then כנסת."""
    out = [surface]
    two = surface[:1] in ("ו", "ש")  # two-letter prefixes start with ו or ש: וב, של, שב
    for k in (1, 2) if two else (1,):
        rest = surface[k:]
        if len(rest) >= 2 and all(ch in PREFIXES and ch != "ה" for ch in surface[:k]):
            out += ["ה" + rest, rest] if surface[k - 1] in "בלכ" else [rest]
    return out


def fold(name: str) -> str:
    """Names compared without geresh or quotes: סמוטריץ' = סמוטריץ."""
    return " ".join(re.sub("['\"׳״`]", "", name).split())


class Linker:
    """Unambiguous links. A person: the full name, or a last name alone, that belongs to exactly
    one person holding a position at that date. A party: an exact faction name."""

    def __init__(self, conn: Any):
        self.people = People.load(conn)
        self.full_names = dict(conn.execute("select person_id, full_name from marts.dim_person"))
        self.by_name: dict[str, set[int]] = {}
        for pid, (first, last) in self.people.names.items():
            for key in (f"{first} {last}", last):
                if key.strip():
                    self.by_name.setdefault(fold(key), set()).add(pid)
        self.factions: dict[str, int] = {}
        for fid, name in conn.execute(
            "select distinct faction_id, faction_name from marts.fct_person_position"
            " where faction_id is not null"
        ):
            self.factions.setdefault(fold(name), fid)
        self.memo: dict[tuple[str, str, int | None], tuple] = {}

    def person(self, name: str, when: Any) -> int | None:
        pid, conf = self.people.resolve(ALIASES.get(name, name), when, set())
        if pid is not None and conf >= 0.6:
            return pid
        active = {p for p in self.by_name.get(fold(name), set()) if self.people.active(p, when)}
        return next(iter(active)) if len(active) == 1 else None

    def link(self, kind: str, surface: str, when: Any) -> tuple[str, str | None, int | None]:
        """(normalized, ref_table, ref_id)."""
        key = (kind, surface, when.year if when else None)
        if key not in self.memo:
            self.memo[key] = (surface, None, None)
            for name in candidates(surface):
                if kind == "person" and (pid := self.person(name, when)) is not None:
                    self.memo[key] = (self.full_names.get(pid, name), "dim_person", pid)
                    break
                if kind == "org" and fold(name) in self.factions:
                    self.memo[key] = (name, "dim_faction", self.factions[fold(name)])
                    break
        return self.memo[key]


def entities(
    text: str, found: list[dict[str, Any]], linker: Linker, when: Any
) -> list[tuple[str, str, str, str | None, int | None, float]]:
    """(type, surface, normalized, ref_table, ref_id, score) for one text."""
    out = []
    for e in found:
        kind = TYPES.get(e["entity_group"])
        surface = " ".join(text[e["start"] : e["end"]].split()).strip("\"'()[],.:;-–")
        if kind and len(surface) >= 2 and e["score"] >= MIN_SCORE:
            out.append(
                (kind, surface, *linker.link(kind, surface, when), round(float(e["score"]), 3))
            )
    return out


def merge_prefixes(conn: Any) -> int:
    """Unlinked names written with a prefix take the form without it when that form was also
    found: בישראל, לישראל → ישראל; בחוק הביטוח הלאומי → חוק הביטוח הלאומי."""
    names = {
        (t, n)
        for t, n in conn.execute(
            "select distinct type, normalized from text.chunk_entities"
            " where source = 'ner' and ref_id is null"
        )
    }
    pairs = [
        (t, n, base)
        for t, n in names
        for base in [next((c for c in candidates(n)[1:] if len(c) >= 3 and (t, c) in names), None)]
        if base
    ]
    with conn.cursor() as cur:
        cur.execute("create temp table _pre (type text, name text, base text) on commit drop")
        with cur.copy("copy _pre from stdin") as copy:
            for row in pairs:
                copy.write_row(row)
        cur.execute(
            """update text.chunk_entities e set normalized = p.base from _pre p
               where e.source = 'ner' and e.ref_id is null
                 and e.type = p.type and e.normalized = p.name"""
        )
        return cur.rowcount


def run_ner(
    knessets: list[int] | None, limit: int | None = None, tagger: Tagger | None = None
) -> dict[str, int]:
    """Tag the chunks in scope not read yet. Returns counts."""
    migrate()
    totals = {"chunks": 0, "entities": 0, "linked": 0}
    with logs.run("ner", knessets=knessets, limit=limit) as run, connect() as conn:
        linker = Linker(conn)
        tag = tagger or load_tagger(detect_device(settings().device))
        while limit is None or totals["chunks"] < limit:
            take = BATCH if limit is None else min(BATCH, limit - totals["chunks"])
            rows = conn.execute(
                """select c.chunk_id, c.text, d.doc_date from text.chunks c
                   join text.documents d using (doc_id)
                   left join text.chunk_ner n using (chunk_id)
                   where n.chunk_id is null and c.meta->>'kind' <> 'header'
                     and (%(k)s::int[] is null or d.knesset_num = any(%(k)s))
                   order by c.chunk_id desc limit %(n)s""",
                {"k": knessets, "n": take},
            ).fetchall()
            if not rows:
                break
            texts = [ner_text(t) for _, t, _ in rows]
            found = tag(texts)
            ents, done = [], []
            for (chunk_id, _, when), text, f in zip(rows, texts, found, strict=True):
                es = entities(text, f, linker, when)
                ents += [(chunk_id, *e) for e in es]
                done.append((chunk_id, MODEL, len(es)))
            ids = [r[0] for r in rows]
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "delete from text.chunk_entities where source = 'ner' and chunk_id = any(%s)",
                    (ids,),
                )
                with cur.copy(
                    "copy text.chunk_entities (chunk_id, type, surface, normalized, ref_table,"
                    " ref_id, confidence, source) from stdin"
                ) as copy:
                    for e in ents:
                        copy.write_row((*e, "ner"))
                with cur.copy(
                    "copy text.chunk_ner (chunk_id, model, n_entities) from stdin"
                ) as copy:
                    for d in done:
                        copy.write_row(d)
            totals["chunks"] += len(rows)
            totals["entities"] += len(ents)
            totals["linked"] += sum(e[5] is not None for e in ents)
            log.info("ner_progress", **totals)
        totals["prefixes_merged"] = merge_prefixes(conn)
        run.rows_in, run.rows_out = totals["chunks"], totals["entities"]
        log.info("ner_done", **totals)
    return totals
