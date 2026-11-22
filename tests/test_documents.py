import asyncio
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import docx
import httpx
import pytest

from knesset import documents as d
from knesset.config import settings
from knesset.db import connect
from knesset.odata import OData

HAS_SOFFICE = shutil.which(settings().soffice) is not None
FS = "https://fs.knesset.gov.il/25/Committees"


def make_docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    document = docx.Document()
    for p in paragraphs:
        document.add_paragraph(p)
    if table:
        t = document.add_table(rows=len(table), cols=len(table[0]))
        for i, row in enumerate(table):
            for j, value in enumerate(row):
                t.cell(i, j).text = value
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def to_doc(data: bytes, tmp: Path) -> bytes:
    """Convert .docx bytes to a legacy .doc with LibreOffice (test helper)."""
    (tmp / "x.docx").write_bytes(data)
    subprocess.run(
        [
            settings().soffice,
            "--headless",
            "--convert-to",
            "doc",
            "--outdir",
            str(tmp),
            str(tmp / "x.docx"),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return (tmp / "x.doc").read_bytes()


def test_clean():
    header = "הכנסת העשרים וחמש - ועדת הכספים"
    chair = '<< יור >> היו"ר משה כהן: << יור >>'
    raw = "\n".join(
        [f"‏{header}", "היו״ר משה כהן:", "אני  פותח\tאת הישיבה.", "- 2 -", chair]
        + [header, "עמוד 3", "דברים", "", "", chair, "עוד דברים"] * 4
    )
    text = d.clean(raw)
    assert header not in text  # repeated 5 times: a running header
    assert 'היו"ר משה כהן:' in text  # gershayim → ", speaker line kept
    assert text.count(chair) == 5  # a chair speaking often is not a running header
    assert "אני פותח את הישיבה." in text
    assert "- 2 -" not in text and "עמוד 3" not in text  # page numbers
    assert "\n\n\n" not in text


def test_clean_removes_control_characters():
    assert (
        d.clean("תקנות\x00 הבריאות\x07\n\x0cסעיף 1") == "תקנות הבריאות\n\nסעיף 1"
    )  # page break → blank line


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")
def test_docx_falls_back_to_soffice(monkeypatch):
    def broken(data):
        raise KeyError("There is no item named 'word/NULL' in the archive")

    slots: asyncio.Queue = asyncio.Queue()
    slots.put_nowait(0)
    data = make_docx(["תקנות הבריאות"])
    monkeypatch.setattr(d, "docx_text", broken)
    assert "תקנות הבריאות" in asyncio.run(d.to_text(data, "docx", slots))
    monkeypatch.setattr(d, "docx_text", lambda data: "  ")  # no body text (e.g. text boxes)
    assert "תקנות הבריאות" in asyncio.run(d.to_text(data, "docx", slots))


def test_detect():
    assert d.detect(make_docx(["x"])) == "docx"
    assert d.detect(b"\xd0\xcf\x11\xe0rest") == "doc"
    assert d.detect(b"{\\rtf1\\ansi") == "rtf"
    assert d.detect(b"%PDF-1.7") == "pdf"
    assert d.detect(b"<!DOCTYPE html>") == "html"


def test_docx_text_keeps_document_order():
    data = make_docx(["פתיחה", "סיום"], table=[["שם", "תפקיד"], ["דנה", 'יו"ר']])
    assert d.docx_text(data).split("\n") == ["פתיחה", "סיום", "שם | תפקיד", 'דנה | יו"ר']


def test_docx_text_reads_content_controls():
    """Speaker names sit in Word content controls (w:sdt), inline and as whole paragraphs."""
    from docx.oxml import parse_xml

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    document = docx.Document()
    document.add_paragraph("פתיחה")
    document.add_paragraph()._p.append(
        parse_xml(
            f"<w:sdt {w}><w:sdtContent><w:r><w:t>"
            "&lt;&lt; דובר &gt;&gt; משה כהן: &lt;&lt; דובר &gt;&gt;"
            "</w:t></w:r></w:sdtContent></w:sdt>"
        )
    )
    document.element.body.insert(
        2,
        parse_xml(
            f"<w:sdt {w}><w:sdtContent><w:p><w:r><w:t>"
            "&lt;&lt; יור &gt;&gt; דנה: &lt;&lt; יור &gt;&gt;"
            "</w:t></w:r></w:p></w:sdtContent></w:sdt>"
        ),
    )
    buf = io.BytesIO()
    document.save(buf)
    assert d.docx_text(buf.getvalue()).split("\n") == [
        "פתיחה",
        "<< דובר >> משה כהן: << דובר >>",
        "<< יור >> דנה: << יור >>",
    ]


@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")
def test_soffice_text(tmp_path):
    data = to_doc(make_docx(["פרוטוקול מס' 12", "היו״ר: שלום לכולם"]), tmp_path)
    assert d.detect(data) == "doc"
    slots: asyncio.Queue = asyncio.Queue()
    slots.put_nowait(0)
    text = asyncio.run(d.soffice_text(data, "doc", slots))
    assert "פרוטוקול מס' 12" in text and "שלום לכולם" in text
    assert slots.qsize() == 1  # slot returned


# ---------------------------------------------------------------- registry + extraction


def doc_row(i: int, session: int, group: tuple[int, str], name: str, app: str) -> dict:
    return {
        "Id": i,
        "CommitteeSessionID": session,
        "GroupTypeID": group[0],
        "GroupTypeDesc": group[1],
        "ApplicationDesc": app,
        "FilePath": f"{FS}/{name}",
    }


PROTOCOL, BACKGROUND = (23, "פרוטוקול ועדה"), (40, "חומר רקע")
RAW = {
    "kns_committeesession": [
        {"Id": 500, "KnessetNum": 25, "CommitteeID": 7, "StartDate": "2025-01-05T10:00:00+02:00"},
        {"Id": 501, "KnessetNum": 19, "CommitteeID": 7, "StartDate": "2014-01-05T10:00:00+02:00"},
    ],
    "kns_documentcommitteesession": [
        doc_row(1, 500, PROTOCOL, "p500.doc", "DOC"),
        doc_row(2, 500, PROTOCOL, "p500.pdf", "PDF"),
        doc_row(3, 500, PROTOCOL, "p500.docx", "DOC"),
        doc_row(4, 500, BACKGROUND, "a.docx", "DOC"),
        doc_row(5, 500, BACKGROUND, "b.doc", "DOC"),
        doc_row(6, 501, PROTOCOL, "p501.pdf", "PDF"),
        doc_row(7, 501, BACKGROUND, "t.xls", "XLS"),
    ],
    "kns_plenumsession": [{"Id": 600, "KnessetNum": 25, "StartDate": "2025-01-06T11:00:00+02:00"}],
    "kns_documentplenumsession": [
        {
            "Id": 8,
            "PlenumSessionID": 600,
            "GroupTypeID": 1,
            "GroupTypeDesc": "דברי הכנסת",
            "ApplicationDesc": "DOC",
            "FilePath": "https://fs.knesset.gov.il/25/Plenum/600.doc",
        },
    ],
    "kns_query": [{"Id": 77, "KnessetNum": 25, "SubmitDate": "2025-02-01T09:00:00"}],
    "kns_documentquerie": [
        {
            "id": 9,
            "queryID": 77,
            "groupTypeID": 38,
            "groupTypeDesc": "שאילתה",
            "applicationDesc": "DOC",
            "filePath": f"{FS}/q77.doc",
        },
    ],
}


def load_raw() -> None:
    with connect() as conn:
        for table, rows in RAW.items():
            conn.execute(
                f'create table raw."{table}" (id bigint primary key, doc jsonb not null,'
                " last_updated timestamptz, loaded_at timestamptz not null default now())"
            )
            for row in rows:
                conn.execute(
                    f'insert into raw."{table}" (id, doc) values (%s, %s)',
                    (row.get("Id", row.get("id")), json.dumps(row, ensure_ascii=False)),
                )


def q(sql: str, params: tuple = ()) -> list[tuple]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def statuses() -> dict[str, str]:
    return dict(q("select doc_id, status from text.documents"))


def test_registry_selects_word_files(db):
    load_raw()
    counts = asyncio.run(d.build_registry([25, 26]))

    assert statuses() == {
        "committee_session:1": "superseded",  # .doc of the same file as the .docx
        "committee_session:2": "superseded",  # PDF, but the protocol exists in Word
        "committee_session:3": "pending",  # .docx wins
        "committee_session:4": "pending",  # different attachments: both kept
        "committee_session:5": "pending",
        "committee_session:6": "skipped_pdf",  # PDF-only protocol
        "committee_session:7": "skipped_other",
        "plenum_session:8": "pending",
        "query:9": "pending",  # camelCase source
    }
    assert counts == {"superseded": 2, "pending": 5, "skipped_pdf": 1, "skipped_other": 1}
    # plenum protocols are filed as "דברי הכנסת"
    assert q("select priority from text.documents where doc_id = 'plenum_session:8'") == [(2,)]
    assert q(
        "select priority, knesset_num, committee_id, doc_date::text from text.documents"
        " where doc_id = 'committee_session:3'"
    ) == [(1, 25, 7, "2025-01-05 10:00:00")]
    assert q("select priority, parent_id from text.documents where doc_id = 'query:9'") == [(4, 77)]

    # Unchanged rows keep their status; a changed URL is extracted again from scratch.
    with connect() as conn:
        conn.execute(
            "update text.documents set status = 'done', attempts = 1"
            " where doc_id in ('committee_session:3', 'committee_session:4')"
        )
        conn.execute(
            "update raw.kns_documentcommitteesession set doc = jsonb_set(doc, '{FilePath}',"
            " to_jsonb(%s::text)) where id = 4",
            (f"{FS}/a_v2.docx",),
        )
    asyncio.run(d.build_registry([25, 26]))
    assert q(
        "select doc_id, status, attempts from text.documents where doc_id in"
        " ('committee_session:3', 'committee_session:4') order by 1"
    ) == [
        ("committee_session:3", "done", 1),
        ("committee_session:4", "pending", 0),
    ]


def test_extract_end_to_end(db, tmp_path):
    load_raw()
    asyncio.run(d.build_registry([25, 26]))
    protocol = make_docx(["ועדת הכספים", "היו״ר משה כהן:", "בוקר טוב."])
    files = {
        "p500.docx": protocol,
        "a.docx": make_docx(["חומר רקע"]),
        "600.doc": make_docx(["דברי הכנסת"]),  # a ".doc" that is really .docx, as on the site
        # served as .doc but really a PDF: confirmed by magic bytes
        "b.doc": to_doc(make_docx(["מסמך ישן"]), tmp_path) if HAS_SOFFICE else b"%PDF-1.4",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if "json" in request.headers.get("accept", ""):
            return httpx.Response(406)  # like fs.knesset.gov.il
        return httpx.Response(200, content=files[name]) if name in files else httpx.Response(404)

    def run(**kw):
        api = OData(transport=httpx.MockTransport(handler), backoff_s=0, retries=1)
        return asyncio.run(d.extract([25, 26], api=api, **kw))

    before = set(Path(tempfile.gettempdir()).glob("knesset-*"))
    totals = run()
    assert totals == {
        "done": 4 if HAS_SOFFICE else 3,
        "retry": 1,
        **({} if HAS_SOFFICE else {"skipped_pdf": 1}),
    }

    text, n_chars, kind = q(
        "select text, n_chars, detected_type from text.documents"
        " where doc_id = 'committee_session:3'"
    )[0]
    assert text == 'ועדת הכספים\nהיו"ר משה כהן:\nבוקר טוב.' and n_chars == len(text)
    assert kind == "docx"
    if HAS_SOFFICE:
        assert (
            "מסמך ישן"
            in q("select text from text.documents where doc_id = 'committee_session:5'")[0][0]
        )

    # The 404 is retried on later runs and fails for good after 3 attempts.
    assert run() == {"retry": 1}
    assert run() == {"failed": 1}
    assert run() == {}
    assert q("select status, attempts from text.documents where doc_id = 'query:9'") == [
        ("failed", 3)
    ]
    assert q("select attempt from ops.failures where step = 'extract' order by id") == [
        (1,),
        (2,),
        (3,),
    ]
    lo_profiles = {p for p in Path(tempfile.gettempdir()).glob("knesset-lo-profile-*")}
    assert set(Path(tempfile.gettempdir()).glob("knesset-*")) - before - lo_profiles == set()

    # --redo docx extracts the done .docx documents again; other types keep their status.
    assert run(redo=["docx"]) == {"done": 3}  # the failed one stays failed
    assert q("select status from text.documents where doc_id = 'committee_session:3'") == [
        ("done",)
    ]
