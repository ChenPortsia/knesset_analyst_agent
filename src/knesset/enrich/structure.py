"""Protocol structure from the documents' own markup; no model involved.

- sections: a protocol split at its agenda markers (`<< נושא >>`, `<< הצח >>`, `<< הלסי >>`),
  with words and turns per speaker, so "how much of the session went to each item, and who
  spoke" is a sum. A section's title is matched to the session's items as in links.py.
- participants: the attendance list at the top of committee protocols (`name | – | role,
  organization` under headings like חברי הוועדה, מוזמנים).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from knesset import log as logs
from knesset.chunks import ANONYMOUS, ATTENDANCE_HEADINGS, People, normalize_name, speaker_finder
from knesset.db import connect, migrate
from knesset.enrich.links import SESSION_TYPES, best_item, session_items

SECTION = re.compile(r"<<\s*(נושא|הצח|הלסי)\s*>>(.+?)<<\s*\1\s*>>", re.S)
MARKUP = re.compile(r"<<\s*([^<>]+?)\s*>>.*?<<\s*\1\s*>>", re.S)  # any tagged span
ATTENDANCE = re.compile(rf"^({'|'.join(ATTENDANCE_HEADINGS)})\s*:", re.M)
MIN_WORDS = 20  # a marker followed by fewer words is a heading for the next marker
LIST_END = ("רשימת הנוכחים", "סדר היום", "סדר-היום")
SKIP_GROUPS = ("רישום פרלמנטרי", "קצרנ", "רשמ")  # the transcription service, not participants
BATCH = 200
COLUMNS = {
    "sections": "doc_id, ord, tag, title, item_type_id, item_id, n_words, n_turns",
    "section_speakers": "doc_id, ord, speaker_raw, role, person_id, n_words, n_turns",
    "participants": "doc_id, ord, name, grp, description, organization, person_id",
}
PROTOCOLS = "(d.group_type like '%%פרוטוקול%%' or d.group_type = 'דברי הכנסת')"

log = structlog.get_logger()


@dataclass
class Section:
    tag: str | None
    title: str | None
    speakers: dict[tuple[str, str], list[int]] = field(default_factory=dict)  # → [words, turns]

    @property
    def n_words(self) -> int:
        return sum(w for w, _ in self.speakers.values())


def count_words(lines: list[str]) -> int:
    return len(MARKUP.sub(" ", "\n".join(lines)).split())


def add(into: dict[tuple[str, str], list[int]], other: dict[tuple[str, str], list[int]]) -> None:
    for key, (w, t) in other.items():
        acc = into.setdefault(key, [0, 0])
        acc[0] += w
        acc[1] += t


def header_end(text: str, find: Callable[[str], Any]) -> int:
    """Offset of the first speaker line: everything before it is the header."""
    offset = 0
    for line in text.split("\n"):
        if find(line):
            return offset
        offset += len(line) + 1
    return len(text)


def split_sections(text: str) -> list[Section]:
    """Sections of a protocol in order. The opening (before the first marker in the body) is
    titled by the last marker between the attendance list and the first speaker, if any.
    Words before a speaker line inside a section belong to whoever spoke last. A segment of
    fewer than MIN_WORDS (a marker straight after a marker) merges into the next one; of the
    merged titles, a נושא title wins."""
    find = speaker_finder(text, protocol=True)
    if find is None:
        return []
    body_start = header_end(text, find)
    listed = ATTENDANCE.search(text[:body_start])
    attendance = listed.start() if listed else -1
    markers = list(SECTION.finditer(text))
    opening = [m for m in markers if attendance < m.start() < body_start]
    body = [m for m in markers if m.start() >= body_start]
    bounds = [(body_start, opening[-1] if opening else None)] + [(m.end(), m) for m in body]
    ends = [m.start() for m in body] + [len(text)]

    segments: list[tuple[re.Match | None, dict]] = []
    speaker: tuple[str, str] | None = None
    for (start, marker), end in zip(bounds, ends, strict=True):
        speakers: dict[tuple[str, str], list[int]] = {}
        for line in text[start:end].split("\n"):
            if sp := find(line):
                speaker = (sp[1], sp[0])
                speakers.setdefault(speaker, [0, 0])[1] += 1
            elif speaker and (n := count_words([line])):
                speakers.setdefault(speaker, [0, 0])[0] += n
        segments.append((marker, speakers))

    sections: list[Section] = []
    titles: list[re.Match] = []
    acc: dict[tuple[str, str], list[int]] = {}
    for i, (marker, speakers) in enumerate(segments):
        titles += [marker] if marker else []
        add(acc, speakers)
        if sum(w for w, _ in acc.values()) < MIN_WORDS and i < len(segments) - 1:
            continue
        chosen = next((m for m in titles if m.group(1) == "נושא"), titles[0] if titles else None)
        tag, title = (
            (chosen.group(1), " ".join(chosen.group(2).split())) if chosen else (None, None)
        )
        sections.append(Section(tag, title, acc))
        titles, acc = [], {}
    return sections


@dataclass
class Participant:
    name: str
    grp: str | None
    description: str | None
    organization: str | None


def parse_participants(header: str) -> list[Participant]:
    """The attendance list: from the first attendance heading to the end of the list. Headings
    end with ':'; entries are `name | – | description` or `name – description`."""
    out: list[Participant] = []
    grp: str | None = None
    inside = False
    for line in (x.strip() for x in header.split("\n")):
        heading = line.endswith(":") and len(line) < 60
        if not inside:
            inside = heading and line.startswith(ATTENDANCE_HEADINGS)
            grp = None if not inside or line.startswith("נכחו") else line.rstrip(":").strip()
            continue
        if line.startswith(LIST_END) or "<<" in line or len(line) > 200:
            break
        if heading:
            grp = line.rstrip(":").strip()
        elif line and not (grp or "").startswith(SKIP_GROUPS):
            parts = [x.strip() for x in line.split("|") if x.strip() not in ("", "–", "-")]
            if len(parts) == 1:
                name, _, desc = parts[0].partition(" – ")
            else:
                name, desc = parts[0], ", ".join(parts[1:])
            desc = desc.strip() or None
            org = desc.rsplit(",", 1)[1].strip() if desc and "," in desc else None
            if name.strip():
                out.append(Participant(name.strip(), grp, desc, org or None))
    return out


def build(knessets: list[int] | None) -> dict[str, int]:
    """Rebuild sections, section speakers and participants for the protocols in scope."""
    migrate()
    params = {"k": knessets}
    scope = f"""{PROTOCOLS} and d.status = 'done'
                and (%(k)s::int[] is null or d.knesset_num = any(%(k)s))"""
    totals = {"protocols": 0, "sections": 0, "section_speakers": 0, "participants": 0}
    with logs.run("structure", knessets=knessets) as run, connect() as conn:
        items = session_items(conn)
        people = People.load(conn)
        ids = [
            r[0] for r in conn.execute(f"select doc_id from text.documents d where {scope}", params)
        ]
        for table in ("sections", "participants"):  # section_speakers go with their section
            conn.execute(
                f"""delete from text.{table} t using text.documents d
                    where t.doc_id = d.doc_id and {scope}""",
                params,
            )
        for i in range(0, len(ids), BATCH):
            docs = conn.execute(
                """select doc_id, parent_type, parent_id, doc_date, text from text.documents
                   where doc_id = any(%s)""",
                (ids[i : i + BATCH],),
            ).fetchall()
            sections, speakers, participants = [], [], []
            for doc_id, ptype, pid, when, text in docs:
                find = speaker_finder(text, protocol=True)
                header = text[: header_end(text, find)] if find else text
                plist = parse_participants(header)
                present = {normalize_name(x.name) for x in plist}
                for k, x in enumerate(plist):
                    person, conf = people.resolve(x.name, when, present)
                    member = "חבר" in (x.grp or "")
                    keep = person if conf >= (0.6 if member else 0.9) else None
                    participants.append(
                        (doc_id, k, x.name, x.grp, x.description, x.organization, keep)
                    )
                persons: dict[str, int | None] = {}  # resolved as in resolve_speakers
                session = items.get((SESSION_TYPES[ptype], pid), [])
                for k, s in enumerate(split_sections(text)):
                    item = best_item(s.title, session) if s.title else None
                    turns = sum(t for _, t in s.speakers.values())
                    sections.append(
                        (doc_id, k, s.tag, s.title, *(item or (None, None)), s.n_words, turns)
                    )
                    by_name: dict[str, list] = {}  # דובר and דובר_המשך are one speaker
                    for (name, role), (w, t) in s.speakers.items():
                        row = by_name.setdefault(name, [role, 0, 0])
                        row[1], row[2] = row[1] + w, row[2] + t
                    for name, (role, w, t) in by_name.items():
                        if name not in persons:
                            anonymous = normalize_name(name) in ANONYMOUS
                            found = (None, 0) if anonymous else people.resolve(name, when, present)
                            persons[name] = found[0]
                        speakers.append((doc_id, k, name, role, persons[name], w, t))
            with conn.cursor() as cur:
                for table, rows in zip(COLUMNS, (sections, speakers, participants), strict=True):
                    with cur.copy(f"copy text.{table} ({COLUMNS[table]}) from stdin") as copy:
                        for row in rows:
                            copy.write_row(row)
                    totals[table] += len(rows)
            totals["protocols"] += len(docs)
            log.info("structure_progress", **totals)
        run.rows_in, run.rows_out = totals["protocols"], totals["sections"]
        log.info("structure_done", **totals)
    return totals
