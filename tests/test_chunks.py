import hashlib
import json
from datetime import datetime

from knesset import chunks as c
from knesset.db import connect
from knesset.embed import DIM, build_index, embed_chunks


def words(text: str) -> int:
    """Stand-in for the bge-m3 tokenizer (not downloadable in tests)."""
    return len(text.split())


SENTENCE = "זה משפט של עשר מילים בדיוק בשביל הבדיקה הזאת עכשיו."
assert words(SENTENCE) == 10
HEADER = """הכנסת העשרים-וחמש
פרוטוקול מס' 490
מישיבת ועדת הכלכלה
סדר היום:
<< נושא >> מכסות לייבוא דבש << נושא >>
נכחו:
חברי הוועדה:
דוד ביטן – היו"ר
אלון שוסטר
מוזמנים:
ד"ר אסף לוי | – | סמנכ"ל, משרד החקלאות"""
LONG_TURN = "\n".join([" ".join([SENTENCE] * 12)] * 10)  # 1,200 words in 10 paragraphs
PROTOCOL = f"""{HEADER}
<< יור >> היו"ר דוד ביטן: << יור >>
בוקר טוב. אני פותח את הישיבה.
<< דובר >> אלון שוסטר (המחנה הממלכתי): << דובר >>
{LONG_TURN}
<< קריאה >> שלום דנינו (הליכוד): << קריאה >>
נכון.
<< אורח >> אסף לוי: << אורח >>
התשובה שלנו פשוטה. {SENTENCE}
<< קריאה >> קריאות: << קריאה >>
לא נכון.
<< הפסקה >> (הישיבה נפסקה בשעה 12:00) << הפסקה >>"""


def test_protocol_chunks():
    chunks = c.chunk_text(PROTOCOL, words, protocol=True)
    kinds = [ch.kind for ch in chunks]
    assert kinds[0] == "header" and set(kinds[1:]) == {"turns"}
    assert "נכחו:" in chunks[0].text and "<< יור >>" not in chunks[0].text

    # the chair's short turn is packed with nothing else before the long turn starts
    assert [s[0] for s in chunks[1].speakers] == ['היו"ר דוד ביטן']
    long = [ch for ch in chunks if ch.speakers and ch.speakers[0][0].startswith("אלון")]
    assert len(long) >= 3
    for piece in long:
        assert piece.text.startswith("<< דובר >> אלון שוסטר")  # speaker line repeated
        assert piece.n_tokens <= c.TARGET + 10
        assert all(line.endswith((".", ">>")) for line in piece.text.split("\n"))  # whole sentences
    first, second = (ch.text.split("\n", 1)[1] for ch in long[:2])
    assert first.endswith(SENTENCE) and second.startswith(SENTENCE)  # overlap

    # the last two short turns share a chunk; topic tags are not speakers
    assert [s[0] for s in chunks[-1].speakers] == ["שלום דנינו (הליכוד)", "אסף לוי", "קריאות"]
    assert "(הישיבה נפסקה" in chunks[-1].text  # a break is text, not a speaker
    assert not any(s[0].startswith("מכסות") for ch in chunks for s in ch.speakers)


def test_untagged_protocols_and_other_documents():
    text = "\n".join(
        ["הישיבה נפתחה.", "משה כהן:", "דברים.", "דנה לוי:", "תשובה.", "משה כהן:", "סיום."]
    )
    assert [ch.kind for ch in c.chunk_text(text, words, protocol=True)] == ["header", "turns"]
    # the same 'name:' lines in a bill are not speakers
    assert [ch.kind for ch in c.chunk_text(text, words, protocol=False)] == ["paragraphs"]


def test_paragraph_windows_overlap():
    text = "\n".join([" ".join([SENTENCE] * 5)] * 30)  # 1,500 words in 50-word paragraphs
    chunks = c.chunk_text(text, words)
    assert len(chunks) == 4
    assert all(ch.n_tokens <= c.TARGET for ch in chunks)
    tail, head = chunks[0].text[-200:], chunks[1].text[:200]
    assert SENTENCE in tail and head.startswith(SENTENCE)  # repeated sentences
    assert sum(ch.n_tokens for ch in chunks) > 1500  # the overlap is counted in each window


def test_names_and_attendance():
    assert c.normalize_name('היו"ר דוד ביטן') == "דוד ביטן"
    assert c.normalize_name("אלון שוסטר (המחנה הממלכתי)") == "אלון שוסטר"
    assert c.normalize_name('סגן השר ד"ר משה כהן – משרד הבריאות') == "משה כהן"
    assert c.attendance_names(HEADER) == {"דוד ביטן", "אלון שוסטר", "אסף לוי"}


def test_resolve_speakers():
    when = datetime(2024, 10, 29)
    people = c.People(
        variants={
            "דוד ביטן": {1},
            "ביטן דוד": {1},
            "אלון שוסטר": {2},
            "משה כהן": {3, 4},
            "יצחק רבין": {5},
            "איתמר בן גביר": {6},
            "יצחק וסרלאוף": {7},
        },
        names={
            1: ("דוד", "ביטן"),
            2: ("אלון", "שוסטר"),
            3: ("משה", "כהן"),
            4: ("משה", "כהן"),
            5: ("יצחק", "רבין"),
            6: ("איתמר", "בן גביר"),
            7: ("יצחק", "וסרלאוף"),
        },
        periods={
            1: [(datetime(2022, 11, 15), None)],
            2: [(datetime(2022, 11, 15), None)],
            3: [(datetime(2022, 11, 15), None)],
            4: [(datetime(2021, 1, 1), None)],
            5: [(datetime(1992, 7, 13), datetime(1995, 11, 4))],
            6: [(datetime(2022, 11, 15), None)],
            7: [(datetime(2022, 11, 15), None)],
        },
    )
    present = {"דוד ביטן", "אלון שוסטר"}
    assert people.resolve('היו"ר דוד ביטן', when, present) == (1, 1.0)
    assert people.resolve("אלון שוסטר", when, set()) == (2, 0.9)  # active, not in the list
    assert people.resolve("ביטן", when, present) == (1, 0.7)  # last name, present
    assert people.resolve("יצחק רבין", when, present) == (5, 0.6)  # unique, not active
    assert people.resolve("משה כהן", when, present) == (None, 0.0)  # two active people
    assert people.resolve("אסף לוי", when, present) == (None, 0.0)  # invitee, not in marts
    # a minister's full title around the name
    assert people.resolve("השר לביטחון לאומי איתמר בן גביר", when, present) == (6, 0.8)
    assert people.resolve("שר הבריאות משה כהן", when, present) == (None, 0.0)  # ambiguous
    title = "שר הנגב, הגליל והחוסן הלאומי יצחק שמעון וסרלאוף"  # a comma inside the title
    assert people.resolve(title, when, present) == (7, 0.8)


# ---------------------------------------------------------------- database


def setup_db(texts: dict[str, str]) -> None:
    with connect() as conn:
        conn.execute("""create table marts.dim_person as
            select 1::bigint person_id, array['דוד ביטן', 'ביטן דוד'] name_variants,
                   'דוד' first_name, 'ביטן' last_name
            union all select 2, array['אלון שוסטר', 'שוסטר אלון'], 'אלון', 'שוסטר'""")
        conn.execute("""create table marts.fct_person_position as
            select 1::bigint person_id, '2022-11-15'::timestamp start_date,
                   null::timestamp finish_date
            union all select 2, '2022-11-15', null""")
        for doc_id, text in texts.items():
            conn.execute(
                """insert into text.documents (doc_id, source_table, parent_type, url, status,
                       knesset_num, doc_date, group_type, text, content_hash)
                   values (%s, 'x', 'committee_session', 'u', 'done', 25, '2024-10-29',
                           'פרוטוקול ועדה', %s, %s)""",
                (doc_id, text, hashlib.sha256(text.encode()).hexdigest()),
            )


def q(sql: str, params: tuple = ()) -> list[tuple]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def test_hebrew_prefix_search(db):
    assert q("select text.he_strip('ובכנסת הכנסת כנסת משה ומשה')") == [("נסת נסת נסת משה משה",)]
    setup_db({"d:1": "דיון והכנסת אישרה את החוק."})
    c.chunk_documents([25], count=words)
    plain = "tsv @@ plainto_tsquery('simple', 'כנסת')"
    stripped = "tsv_stripped @@ plainto_tsquery('simple', text.he_strip('כנסת'))"
    assert q(f"select count(*) from text.chunks where {plain}") == [(0,)]
    assert q(f"select count(*) from text.chunks where {stripped}") == [(1,)]


def test_chunk_and_embed(db):
    setup_db({"d:1": PROTOCOL, "d:2": PROTOCOL.replace("490", "491"), "d:3": PROTOCOL})
    totals = c.chunk_documents([25], count=words)
    assert totals["docs"] == 3 and totals["speaker_chunks"] > 0
    rows = q(
        "select ord, speaker_raw, person_id, speaker_confidence, meta from text.chunks"
        " where doc_id = 'd:1' order by ord"
    )
    assert [r[0] for r in rows] == list(range(len(rows)))  # contiguous ord
    assert rows[0][4]["kind"] == "header" and rows[0][1] is None
    assert rows[1][1:4] == ('היו"ר דוד ביטן', 1, 1.0)
    assert {r[2] for r in rows if r[1] and r[1].startswith("אלון")} == {2}
    assert [r[3] for r in rows if r[1] == "אסף לוי"] == [None]  # unresolved: no confidence

    # nothing changed → nothing to do; changed text → chunks rebuilt
    assert c.chunk_documents([25], count=words)["docs"] == 0
    with connect() as conn:
        conn.execute(
            "update text.documents set text = 'טקסט חדש.', content_hash = 'h2' where doc_id = 'd:2'"
        )
    assert c.chunk_documents([25], count=words)["docs"] == 1
    assert q("select count(*), min(text) from text.chunks where doc_id = 'd:2'") == [
        (1, "טקסט חדש.")
    ]

    # better matching later: re-resolve in place, chunks untouched
    ids_before = q("select array_agg(chunk_id order by chunk_id) from text.chunks")
    baseline = c.resolve_speakers([25])["resolved"]
    with connect() as conn:
        conn.execute(
            "update marts.dim_person set name_variants = array_append(name_variants, 'אסף לוי')"
            " where person_id = 2"
        )
    assert c.resolve_speakers([25])["resolved"] > baseline
    assert q("select distinct person_id from text.chunks where speaker_raw = 'אסף לוי'") == [(2,)]
    assert q("select array_agg(chunk_id order by chunk_id) from text.chunks") == ids_before

    calls: list[int] = []

    def fake_encoder(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        return [[1.0 if i == hash(t) % DIM else 0.0 for i in range(DIM)] for t in texts]

    n_chunks, n_distinct = q("select count(*), count(distinct chunk_hash) from text.chunks")[0]
    assert n_distinct < n_chunks  # d:3 repeats d:1
    totals = embed_chunks([25], batch=4, encoder=fake_encoder)
    assert totals["embedded"] == sum(calls) == n_distinct
    assert totals["reused"] == n_chunks - n_distinct
    assert q("select count(*) from text.chunks where embedding is null") == [(0,)]
    assert embed_chunks([25], encoder=fake_encoder)["embedded"] == 0

    build_index()
    assert q("select count(*) from pg_indexes where indexname = 'chunks_embedding_hnsw'") == [(1,)]
    nearest = q("""select doc_id, ord from text.chunks
                   order by embedding <=> (select embedding from text.chunks
                                           where doc_id = 'd:2') limit 1""")
    assert nearest == [("d:2", 0)]
    assert json.loads(json.dumps(rows[0][4]))["speakers"] == []
