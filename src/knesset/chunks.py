"""Speaker-aware chunking of document text, and speaker resolution to marts.dim_person.

Protocols: the Knesset marks speakers with tags, e.g. `<< דובר >> משה כהן (הליכוד): << דובר >>`
(tags: יור, דובר, אורח, קריאה, דובר_המשך, …); older protocols just have short lines ending
with ':'. Chunks pack whole speaker turns up to TARGET tokens; a longer turn is split at
sentence ends with OVERLAP tokens repeated. Other documents: paragraph windows, same sizes.
Every chunk keeps its speaker lines, so the text reads on its own.
"""

import hashlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache

import psycopg
import structlog
from psycopg.types.json import Jsonb

from knesset import log as logs
from knesset.db import connect, migrate

TARGET = 500  # tokens per chunk
OVERLAP = 60  # tokens repeated when a turn or paragraph is split
MAX_SPEAKER_LINE = 80  # characters

log = structlog.get_logger()

TAGGED = re.compile(r"^<<\s*([^<>]+?)\s*>>\s*(.+?)\s*:?\s*<<\s*\1\s*>>\s*:?$")
# Tags that mark structure, not a speaker: topic, vote, break, end, chair change, …
STRUCTURAL_TAGS = {"נושא", "הצח", "הפסקה", "סיום", "הלסי", "אחרי_כן", "מנהל", "שאילתה"}
ANONYMOUS = {"קריאה", "קריאות"}  # a call from the floor, no name
UNTAGGED = re.compile(rf"^[^.?!:|<>]{{2,{MAX_SPEAKER_LINE}}}:$")
SENTENCE_END = re.compile(r"(?<=[.?!])\s+")
TITLES = re.compile(
    r"^(היו\"ר|יו\"ר|היושב-ראש|היושבת-ראש|השר|השרה|סגן השר|סגנית השר|שר|שרת|ח\"כ|חה\"כ|"
    r"ד\"ר|דוקטור|עו\"ד|פרופ'|פרופסור|רו\"ח|מר|גב'|הרב|ראש הממשלה|מ\"מ היו\"ר)\s+"
)
ATTENDANCE_HEADINGS = ("חברי הוועדה", "חברי הכנסת", "מוזמנים", "נכחו")


def count_tokens_bge(text: str) -> int:
    return len(_tokenizer().encode(text, add_special_tokens=False).ids)


@cache
def _tokenizer():
    from tokenizers import Tokenizer  # downloads BAAI/bge-m3's tokenizer once (cached)

    return Tokenizer.from_pretrained("BAAI/bge-m3")


# ---------------------------------------------------------------- parsing


@dataclass
class Turn:
    speaker: str | None  # as written, None for the header and narration
    role: str | None  # protocol tag, or "header" / "line"
    lines: list[str] = field(default_factory=list)


NOT_SPEAKERS = (
    "סדר היום",
    "סדר-היום",
    "נכחו",
    "חברי הוועדה",
    "חברי הכנסת",
    "מוזמנים",
    "ייעוץ משפטי",
    "מנהל",
    "רישום פרלמנטרי",
    "קצרנ",
    "רשמ",
    "הגדרות",
)


def tagged_speaker(line: str) -> tuple[str, str] | None:
    """(role, name) for a tagged speaker line; topic tags are not speakers."""
    if m := TAGGED.match(line):
        role, name = m.group(1).strip(), m.group(2).strip().rstrip(":").strip()
        return None if role in STRUCTURAL_TAGS else (role, name)
    return None


def untagged_speaker(line: str) -> tuple[str, str] | None:
    """(\"line\", name) for a short 'name:' line, as in older protocols without tags."""
    if UNTAGGED.match(line) and len(line.split()) <= 8 and not line.startswith(NOT_SPEAKERS):
        return "line", line.rstrip(":").strip()
    return None


def speaker_finder(text: str, protocol: bool) -> Callable[[str], tuple[str, str] | None] | None:
    """Tagged speaker lines when the text has them; 'name:' lines only in protocols."""
    lines = text.split("\n")
    if sum(1 for line in lines if tagged_speaker(line)) >= 3:
        return tagged_speaker
    if protocol and sum(1 for line in lines if untagged_speaker(line)) >= 3:
        return untagged_speaker
    return None


def parse_turns(text: str, find: Callable[[str], tuple[str, str] | None]) -> list[Turn]:
    """Split a protocol into the header and speaker turns, in order."""
    turns = [Turn(None, "header")]
    for line in text.split("\n"):
        if sp := find(line):
            turns.append(Turn(sp[1], sp[0], [line]))
        else:
            turns[-1].lines.append(line)
    return [t for t in turns if any(x.strip() for x in t.lines)]


# ---------------------------------------------------------------- chunking


@dataclass
class Chunk:
    text: str
    n_tokens: int
    speakers: list[tuple[str, str, int]]  # (name, role, tokens) per turn in the chunk
    kind: str  # header, turns, paragraphs


def windows(paragraphs: list[str], count: Callable[[str], int]) -> Iterator[tuple[str, int]]:
    """Pack sentences into ~TARGET-token windows, keeping paragraph breaks, and start each
    window with the last ~OVERLAP tokens of whole sentences of the previous one."""
    units: list[tuple[str, int, bool]] = []  # (sentence, tokens, starts a paragraph)
    for p in paragraphs:
        sentences = [x for x in SENTENCE_END.split(p) if x.strip()] or [p]
        units += [(x, count(x), k == 0) for k, x in enumerate(sentences)]

    def join(part: list[tuple[str, int, bool]]) -> str:
        out = part[0][0]
        for text, _, new_paragraph in part[1:]:
            out += ("\n" if new_paragraph else " ") + text
        return out

    i = 0
    while i < len(units):
        j, total = i, 0
        while j < len(units) and (j == i or total + units[j][1] <= TARGET):
            total += units[j][1]
            j += 1
        yield join(units[i:j]), total
        if j >= len(units):
            break
        k, back = j, 0  # start the next window OVERLAP tokens back, never before i + 1
        while k - 1 > i and back + units[k - 1][1] <= OVERLAP:
            k -= 1
            back += units[k][1]
        i = k


def chunk_text(
    text: str, count: Callable[[str], int] = count_tokens_bge, protocol: bool = False
) -> list[Chunk]:
    find = speaker_finder(text, protocol)
    if find is None:
        paragraphs = [p for p in text.split("\n") if p.strip()]
        return [Chunk(t, n, [], "paragraphs") for t, n in windows(paragraphs, count)]

    chunks: list[Chunk] = []
    pending: list[tuple[Turn, str, int]] = []  # whole turns waiting to fill a chunk

    def flush() -> None:
        if pending:
            chunks.append(
                Chunk(
                    "\n".join(body for _, body, _ in pending),
                    sum(n for _, _, n in pending),
                    [(t.speaker, t.role, n) for t, _, n in pending if t.speaker],
                    "turns",
                )
            )
            pending.clear()

    for turn in parse_turns(text, find):
        lines = [x for x in turn.lines if x.strip()]
        body = "\n".join(lines)
        n = count(body)
        if turn.role == "header":
            chunks += [Chunk(t, k, [], "header") for t, k in windows(lines, count)]
        elif n > TARGET:
            flush()
            head, rest = lines[0], lines[1:]  # repeat the speaker line in every piece
            head_n = count(head)
            for t, k in windows(rest or [head], count):
                piece, k = (f"{head}\n{t}", k + head_n) if rest else (t, k)
                chunks.append(Chunk(piece, k, [(turn.speaker, turn.role, k)], "turns"))
        else:
            if pending and sum(k for _, _, k in pending) + n > TARGET:
                flush()
            pending.append((turn, body, n))
    flush()
    return chunks


# ---------------------------------------------------------------- speakers


def normalize_name(name: str) -> str:
    """Drop titles, a trailing role or faction in parentheses or after a dash, and quotes."""
    name = re.sub(r"\(.*?\)", " ", name)
    name = re.split(r"\s[–-]\s|,", name)[0]
    prev = None
    while prev != name:
        prev, name = name, TITLES.sub("", name.strip())
    return " ".join(name.split())


def attendance_names(header: str) -> set[str]:
    """Names listed after the protocol's first attendance heading (members, invitees, staff),
    one per line, the name before a '|' or '–'."""
    names, inside = set(), False
    for line in (x.strip() for x in header.split("\n")):
        if line.rstrip(":").startswith(ATTENDANCE_HEADINGS):
            inside = True
        elif inside and line and not line.endswith(":"):
            names.add(normalize_name(re.split(r"\s*[|–]\s*", line)[0]))
    return {n for n in names if n}


@dataclass
class People:
    """Name variants and first/last names of dim_person, and the periods people held positions."""

    variants: dict[str, set[int]]  # normalized variant → person ids
    names: dict[int, tuple[str, str]]  # person → (first, last), normalized
    periods: dict[int, list[tuple[datetime | None, datetime | None]]]

    def __post_init__(self) -> None:
        self.last_names: dict[str, set[int]] = {}
        self.last_token: dict[str, set[int]] = {}  # last word of the last name → persons
        for pid, (_, last) in self.names.items():
            if last:
                self.last_names.setdefault(last, set()).add(pid)
                self.last_token.setdefault(last.split()[-1], set()).add(pid)

    @classmethod
    def load(cls, conn: psycopg.Connection) -> "People":
        variants: dict[str, set[int]] = {}
        names: dict[int, tuple[str, str]] = {}
        for pid, vs, first, last in conn.execute(
            "select person_id, name_variants, first_name, last_name from marts.dim_person"
        ):
            for v in vs:
                variants.setdefault(normalize_name(v), set()).add(pid)
            names[pid] = (normalize_name(first or ""), normalize_name(last or ""))
        periods: dict[int, list] = {}
        for pid, start, finish in conn.execute(
            "select person_id, start_date, finish_date from marts.fct_person_position"
        ):
            periods.setdefault(pid, []).append((start, finish))
        return cls(variants, names, periods)

    def active(self, pid: int, when: datetime | None) -> bool:
        if when is None:
            return True
        return any(
            (s is None or s <= when) and (f is None or when.date() <= f.date())
            for s, f in self.periods.get(pid, [])
        )

    def resolve(
        self, name: str, when: datetime | None, present: set[str]
    ) -> tuple[int | None, float]:
        """Person id and confidence for a speaker name at a session date."""
        n = normalize_name(name)
        if not n:
            return None, 0.0
        exact = self.variants.get(n, set())
        active = {p for p in exact if self.active(p, when)}
        if len(active) == 1:
            return next(iter(active)), 1.0 if n in present else 0.9
        if len(exact) == 1:
            return next(iter(exact)), 0.6
        # full first and last name inside a longer title ("השר לביטחון לאומי איתמר בן גביר");
        # the whole string, since titles may contain commas ("שר הנגב, הגליל והחוסן …")
        whole = " ".join(re.sub(r"\(.*?\)", " ", name).replace(",", " ").split())
        words, padded = whole.split(), f" {whole} "
        titled = {
            p
            for w in set(words)
            for p in self.last_token.get(w, set())
            if self.active(p, when)
            and f" {self.names[p][1]} " in padded
            and self.names[p][0]
            and all(x in words for x in self.names[p][0].split())
        }
        if len(titled) == 1:
            return next(iter(titled)), 0.8
        # last name only ("השר כהן", "כהן:"), unique among people present and active
        last = n.split()[-1]
        present_last = {
            p
            for p in self.last_names.get(last, set())
            if self.active(p, when) and any(x.endswith(last) for x in present)
        }
        if len(present_last) == 1 and (len(n.split()) == 1 or n in present):
            return next(iter(present_last)), 0.7
        return None, 0.0


# ---------------------------------------------------------------- storage


def chunk_documents(
    knessets: list[int] | None,
    limit: int | None = None,
    count: Callable[[str], int] = count_tokens_bge,
) -> dict[str, int]:
    """(Re)chunk extracted documents whose text changed since they were last chunked, then
    resolve their speakers."""
    migrate()
    totals = {"docs": 0, "chunks": 0}
    chunked: list[str] = []
    with logs.run("chunk", knessets=knessets, limit=limit) as run, connect() as conn:
        docs = conn.execute(
            """select doc_id, text, content_hash,
                      group_type like '%%פרוטוקול%%' or group_type = 'דברי הכנסת' as protocol
               from text.documents
               where status = 'done' and chunked_hash is distinct from content_hash
                 and (%(k)s::int[] is null or knesset_num = any(%(k)s))
               order by priority, knesset_num desc nulls last, doc_date desc nulls last
               limit %(limit)s""",
            {"k": knessets, "limit": limit},
        ).fetchall()
        for doc_id, text, content_hash, protocol in docs:
            rows = []
            for ord_, c in enumerate(chunk_text(text, count, bool(protocol))):
                name, role = max(c.speakers, key=lambda s: s[2])[:2] if c.speakers else (None, None)
                meta = {"kind": c.kind, "speakers": sorted({s[0] for s in c.speakers})}
                digest = hashlib.sha256(c.text.encode()).hexdigest()
                rows.append((doc_id, ord_, c.text, c.n_tokens, digest, name, role, Jsonb(meta)))
            with conn.transaction():
                conn.execute("delete from text.chunks where doc_id = %s", (doc_id,))
                with conn.cursor().copy(
                    "copy text.chunks (doc_id, ord, text, n_tokens, chunk_hash, speaker_raw,"
                    " speaker_role, meta) from stdin"
                ) as copy:
                    for row in rows:
                        copy.write_row(row)
                conn.execute(
                    "update text.documents set chunked_hash = %s where doc_id = %s",
                    (content_hash, doc_id),
                )
            chunked.append(doc_id)
            totals["docs"] += 1
            totals["chunks"] += len(rows)
            if totals["docs"] % 500 == 0:
                log.info("chunk_progress", **totals)
        run.rows_in, run.rows_out = totals["docs"], totals["chunks"]
        log.info("chunk_done", **totals)
    if chunked:
        totals |= resolve_speakers(doc_ids=chunked)
    return totals


def resolve_speakers(
    knessets: list[int] | None = None, doc_ids: list[str] | None = None
) -> dict[str, int]:
    """Set person_id and speaker_confidence on speaker chunks, in place: re-running it after
    better matching never touches chunks or embeddings."""
    migrate()
    params = {"k": knessets, "ids": doc_ids}
    scope = """(%(k)s::int[] is null or d.knesset_num = any(%(k)s))
               and (%(ids)s::text[] is null or c.doc_id = any(%(ids)s))"""
    with logs.run("speakers", knessets=knessets, docs=len(doc_ids or [])) as run, connect() as conn:
        people = People.load(conn)
        headers = dict(
            conn.execute(
                f"""select c.doc_id, c.text from text.chunks c join text.documents d using (doc_id)
                    where c.ord = 0 and c.meta->>'kind' = 'header' and {scope}""",
                params,
            ).fetchall()
        )
        rows = conn.execute(
            f"""select c.chunk_id, c.doc_id, c.speaker_raw, d.doc_date
                from text.chunks c join text.documents d using (doc_id)
                where c.speaker_raw is not null and {scope}
                  and c.speaker_role <> all(%(structural)s)""",
            params | {"structural": list(STRUCTURAL_TAGS)},
        ).fetchall()
        present: dict[str, set[str]] = {}
        memo: dict[tuple[str, str], tuple[int | None, float]] = {}
        out = []
        anonymous = 0
        for chunk_id, doc_id, name, when in rows:
            if normalize_name(name) in ANONYMOUS:
                anonymous += 1
                out.append((chunk_id, None, None))
                continue
            if doc_id not in present:
                present[doc_id] = attendance_names(headers.get(doc_id, ""))
            if (doc_id, name) not in memo:
                memo[doc_id, name] = people.resolve(name, when, present[doc_id])
            pid, conf = memo[doc_id, name]
            out.append((chunk_id, pid, conf if pid is not None else None))
        with conn.cursor() as cur:
            cur.execute(
                "create temp table _spk (chunk_id bigint, person_id bigint, conf real)"
                " on commit drop"
            )
            with cur.copy("copy _spk (chunk_id, person_id, conf) from stdin") as copy:
                for row in out:
                    copy.write_row(row)
            cur.execute(
                """update text.chunks c set person_id = s.person_id, speaker_confidence = s.conf
                   from _spk s where c.chunk_id = s.chunk_id
                     and (c.person_id, c.speaker_confidence)
                         is distinct from (s.person_id, s.conf)"""
            )
        # named speakers only: structural tags and anonymous calls are not people
        totals = {
            "speaker_chunks": len(out) - anonymous,
            "resolved": sum(r[1] is not None for r in out),
        }
        run.rows_in, run.rows_out = totals["speaker_chunks"], totals["resolved"]
        log.info("speakers_resolved", **totals)
    return totals
