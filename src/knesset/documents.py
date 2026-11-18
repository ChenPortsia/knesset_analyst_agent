"""Document registry (text.documents) and Word text extraction. Word files only (PLAN.md §1).

Registry: every KNS_Document* row becomes one text.documents row. Among files with the same
name (same document, different formats) the best Word format wins (docx > doc > rtf); the
rest are `superseded`. PDFs are `superseded` when their parent and document type have a Word
file, else `skipped_pdf`. Other formats are `skipped_other`.

Extraction: download to memory, confirm the format by magic bytes, extract (.docx with
python-docx, .doc/.rtf with LibreOffice headless in a temp dir that is always removed),
clean, store. At most 3 attempts per document.
"""

import asyncio
import hashlib
import io
import re
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

import asyncpg
import docx
import structlog
from docx.table import Table
from docx.text.paragraph import Paragraph

from knesset import log as logs
from knesset.config import settings
from knesset.db import migrate
from knesset.odata import GeoBlockedError, OData
from knesset.sync import db_pool, record_failure

MAX_ATTEMPTS = 3
BATCH = 200
SOFFICE_TIMEOUT_S = 180

log = structlog.get_logger()

# (document table, parent type, parent id key, parent table, date key, committee key)
SOURCES = [
    (
        "kns_documentcommitteesession",
        "committee_session",
        "CommitteeSessionID",
        "kns_committeesession",
        "StartDate",
        "CommitteeID",
    ),
    (
        "kns_documentplenumsession",
        "plenum_session",
        "PlenumSessionID",
        "kns_plenumsession",
        "StartDate",
        None,
    ),
    ("kns_documentbill", "bill", "BillID", "kns_bill", "PublicationDate", "CommitteeID"),
    (
        "kns_documentagenda",
        "agenda",
        "AgendaID",
        "kns_agenda",
        "PresidentDecisionDate",
        "CommitteeID",
    ),
    ("kns_documentquerie", "query", "QueryID", "kns_query", "SubmitDate", None),
    (
        "kns_documentsecondarylaw",
        "secondary_law",
        "SecondaryLawId",
        "kns_secondarylaw",
        "PublicationDate",
        "CommitteeID",
    ),
    (
        "kns_documentisraellaw",
        "israel_law",
        "IsraelLawID",
        "kns_israellaw",
        "PublicationDate",
        None,
    ),
]
CAMEL_CASE = {"kns_documentquerie"}  # KNS_DocumentQuery serves camelCase keys
WORD = ("docx", "doc", "rtf")


def _source_sql(table: str, ptype: str, pid: str, parent: str, date: str, cmt: str | None) -> str:
    def k(name: str) -> str:
        return name[0].lower() + name[1:] if table in CAMEL_CASE else name

    committee = f"(p.doc->>'{cmt}')::bigint" if cmt else "null::bigint"
    return f"""
        select '{ptype}:' || d.id as doc_id, '{table}' as source_table, '{ptype}' as parent_type,
               (d.doc->>'{k(pid)}')::bigint as parent_id,
               (p.doc->>'KnessetNum')::int as knesset_num, {committee} as committee_id,
               (p.doc->>'{date}')::timestamp as doc_date,
               (d.doc->>'{k("GroupTypeID")}')::int as group_type_id,
               d.doc->>'{k("GroupTypeDesc")}' as group_type,
               lower(d.doc->>'{k("ApplicationDesc")}') as app,
               d.doc->>'{k("FilePath")}' as url, d.last_updated as source_updated
        from raw."{table}" d
        left join raw."{parent}" p on p.id = (d.doc->>'{k(pid)}')::bigint"""


REGISTRY_SQL = """
with src as ({sources}),
typed as (
    select *, coalesce(lower(substring(url from '\\.([A-Za-z0-9]+)$')), app) as file_type,
           lower(regexp_replace(url, '\\.[A-Za-z0-9]+$', '')) as stem
    from src where url is not null
),
ranked as (
    select *,
        bool_or(file_type in ('docx', 'doc', 'rtf'))
            over (partition by parent_type, parent_id, group_type_id) as group_has_word,
        row_number() over (
            partition by parent_type, parent_id, group_type_id, stem,
                         file_type in ('docx', 'doc', 'rtf')
            order by array_position(array['docx', 'doc', 'rtf'], file_type),
                     source_updated desc nulls last, doc_id) as rn
    from typed
),
classified as (
    select doc_id, source_table, parent_type, parent_id, knesset_num, committee_id, doc_date,
           group_type_id, group_type, file_type, url, source_updated,
           case when knesset_num = any($1) and parent_type = 'committee_session'
                     and group_type like '%פרוטוקול%' then 1
                when knesset_num = any($1) and parent_type = 'plenum_session'
                     and (group_type like '%פרוטוקול%' or group_type = 'דברי הכנסת') then 2
                when knesset_num = any($1) and parent_type = 'bill' then 3
                else 4 end as priority,
           case when file_type in ('docx', 'doc', 'rtf') and rn = 1 then 'pending'
                when file_type in ('docx', 'doc', 'rtf') then 'superseded'
                when file_type = 'pdf' and group_has_word then 'superseded'
                when file_type = 'pdf' then 'skipped_pdf'
                else 'skipped_other' end as status
    from ranked
)
insert into text.documents as d (doc_id, source_table, parent_type, parent_id, knesset_num,
    committee_id, doc_date, group_type_id, group_type, file_type, url, source_updated,
    priority, status)
select * from classified
on conflict (doc_id) do update set
    source_table = excluded.source_table, parent_type = excluded.parent_type,
    parent_id = excluded.parent_id, knesset_num = excluded.knesset_num,
    committee_id = excluded.committee_id, doc_date = excluded.doc_date,
    group_type_id = excluded.group_type_id, group_type = excluded.group_type,
    file_type = excluded.file_type, url = excluded.url,
    source_updated = excluded.source_updated, priority = excluded.priority,
    -- new or changed files (URL or LastUpdatedDate) are extracted again
    status = case
        when excluded.status <> 'pending' then excluded.status
        when d.status in ('done', 'failed') and d.url = excluded.url
             and d.source_updated is not distinct from excluded.source_updated then d.status
        else 'pending' end,
    attempts = case
        when d.url = excluded.url
             and d.source_updated is not distinct from excluded.source_updated then d.attempts
        else 0 end,
    updated_at = now()
"""


async def build_registry(knessets: list[int]) -> dict[str, int]:
    """Rebuild text.documents from the raw KNS_Document* tables. Returns counts by status."""
    migrate()
    with logs.run("registry", knessets=knessets) as run:
        async with db_pool() as pool:
            existing = {
                r["tablename"]
                for r in await pool.fetch(
                    "select tablename from pg_tables where schemaname = 'raw'"
                )
            }
            parts = [_source_sql(*s) for s in SOURCES if {s[0], s[3]} <= existing]
            await pool.execute(REGISTRY_SQL.format(sources=" union all ".join(parts)), knessets)
            rows = await pool.fetch("select status, count(*) n from text.documents group by 1")
        counts = {r["status"]: r["n"] for r in rows}
        run.rows_out = sum(counts.values())
        log.info("registry_built", sources=len(parts), **counts)
    return counts


# ---------------------------------------------------------------- extraction


def detect(data: bytes) -> str:
    """File format by magic bytes."""
    if data.startswith(b"PK\x03\x04"):
        return "docx"
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        return "doc"
    if data.lstrip().startswith(b"{\\rtf"):
        return "rtf"
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.lstrip()[:1] == b"<":
        return "html"
    return "other"


def docx_text(data: bytes) -> str:
    """Body paragraphs and tables in document order (headers and footers are not in the body)."""
    document = docx.Document(io.BytesIO(data))
    lines = []
    for block in document.element.body.iterchildren():
        if block.tag.endswith("}p"):
            lines.append(Paragraph(block, document).text)
        elif block.tag.endswith("}tbl"):
            for row in Table(block, document).rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(lines)


async def soffice_text(data: bytes, kind: str, slots: asyncio.Queue) -> str:
    """Convert .doc/.rtf to UTF-8 text with LibreOffice; one user profile per worker slot."""
    slot = await slots.get()
    try:
        with tempfile.TemporaryDirectory(prefix="knesset-") as tmp:
            src = Path(tmp) / f"in.{kind}"
            src.write_bytes(data)
            profile = Path(tempfile.gettempdir()) / f"knesset-lo-profile-{slot}"
            proc = await asyncio.create_subprocess_exec(
                settings().soffice,
                "--headless",
                "--norestore",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                "txt:Text (encoded):UTF8",
                "--outdir",
                tmp,
                str(src),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), SOFFICE_TIMEOUT_S)
            except TimeoutError:
                proc.kill()
                raise
            out = Path(tmp) / "in.txt"
            if not out.exists():
                raise RuntimeError(f"soffice produced no text: {err.decode()[-300:]}")
            return out.read_text(encoding="utf-8", errors="replace")
    finally:
        slots.put_nowait(slot)


DIRECTION_MARKS = re.compile("[‎‏‪-‮⁦-⁩﻿]")
PAGE_NUMBER = re.compile(r"^[-–\s]*(עמוד\s*)?\d{1,4}[-–\s]*$")
HEBREW_PUNCT = str.maketrans({"־": "-", "׳": "'", "״": '"', "\xa0": " "})


def clean(text: str) -> str:
    """Normalize whitespace and Hebrew punctuation, drop page numbers and repeated headers or
    footers (short lines seen 5+ times), keep speaker lines (they end with ':')."""
    text = DIRECTION_MARKS.sub("", unicodedata.normalize("NFC", text)).translate(HEBREW_PUNCT)
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
    counts = Counter(line for line in lines if line and len(line) < 60)
    repeated = {line for line, n in counts.items() if n >= 5 and not line.endswith(":")}
    out: list[str] = []
    for line in lines:
        if line in repeated or PAGE_NUMBER.match(line):
            continue
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


async def extract(
    knessets: list[int] | None,
    limit: int | None = None,
    max_priority: int = 4,
    api: OData | None = None,
) -> Counter:
    """Extract pending documents (in `knessets`, or all when None), by priority."""
    migrate()
    with logs.run("extract", knessets=knessets, limit=limit, max_priority=max_priority) as run:
        totals: Counter = Counter()
        async with api or OData() as api, db_pool() as pool:
            slots: asyncio.Queue = asyncio.Queue()
            for i in range(settings().soffice_workers):
                slots.put_nowait(i)
            done_ids: set[str] = set()
            while limit is None or sum(totals.values()) < limit:
                take = BATCH if limit is None else min(BATCH, limit - sum(totals.values()))
                rows = await pool.fetch(
                    """select doc_id, url, attempts from text.documents
                       where status = 'pending' and priority <= $1
                         and ($2::int[] is null or knesset_num = any($2))
                         and not doc_id = any($3)
                       order by priority, knesset_num desc nulls last, doc_date desc nulls last
                       limit $4""",
                    max_priority,
                    knessets,
                    list(done_ids),
                    take,
                )
                if not rows:
                    break
                done_ids.update(r["doc_id"] for r in rows)
                results = await asyncio.gather(
                    *(extract_one(api, pool, slots, dict(r), run.run_id) for r in rows)
                )
                totals.update(results)
                log.info("extract_batch", **totals)
        run.rows_in = sum(totals.values())
        run.rows_out = totals["done"]
    return totals


async def extract_one(
    api: OData, pool: asyncpg.Pool, slots: asyncio.Queue, row: dict, run_id: str
) -> str:
    """Returns the outcome: done, skipped_pdf, skipped_other, retry or failed."""
    attempts = row["attempts"] + 1
    try:
        # The client defaults to Accept: application/json; the file server answers 406 to that.
        data = (await api.fetch(row["url"], headers={"Accept": "*/*"})).content
        kind = detect(data)
        if kind in ("pdf", "other"):
            status = "skipped_pdf" if kind == "pdf" else "skipped_other"
            await pool.execute(
                """update text.documents set status = $2, detected_type = $3, attempts = $4,
                       error = null, updated_at = now() where doc_id = $1""",
                row["doc_id"],
                status,
                kind,
                attempts,
            )
            return status
        if kind == "html":
            raise RuntimeError("got an HTML page instead of a file")
        raw = await (
            asyncio.to_thread(docx_text, data)
            if kind == "docx"
            else soffice_text(data, kind, slots)
        )
        text = clean(raw)
        if not text:
            raise RuntimeError("no text extracted")
        await pool.execute(
            """update text.documents set status = 'done', detected_type = $2, text = $3,
                   n_chars = $4, content_hash = $5, attempts = $6, error = null,
                   extracted_at = now(), updated_at = now() where doc_id = $1""",
            row["doc_id"],
            kind,
            text,
            len(text),
            hashlib.sha256(text.encode()).hexdigest(),
            attempts,
        )
        return "done"
    except GeoBlockedError:
        raise
    except Exception as e:
        failed = attempts >= MAX_ATTEMPTS
        await pool.execute(
            """update text.documents set status = $2, attempts = $3, error = $4,
                   updated_at = now() where doc_id = $1""",
            row["doc_id"],
            "failed" if failed else "pending",
            attempts,
            repr(e)[:500],
        )
        await record_failure(run_id, row["doc_id"], e, step="extract", attempt=attempts)
        log.warning("extract_failed", doc_id=row["doc_id"], attempt=attempts, error=repr(e)[:200])
        return "failed" if failed else "retry"
