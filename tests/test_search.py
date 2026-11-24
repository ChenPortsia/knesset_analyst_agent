import hashlib

import pytest

from knesset.db import connect
from knesset.search import Filters, fuse, search

# A tiny "semantic" space: words of one concept share a dimension.
CONCEPTS = {"רכבת": 1, "תחבורה": 1, "אוטובוס": 1, "תקציב": 2, "כסף": 2, "חינוך": 3}


def encode(texts):
    out = []
    for t in texts:
        v = [0.0] * 1024
        for word in t.split():
            for concept, dim in CONCEPTS.items():
                if concept in word:
                    v[dim] += 1
        v[1023] = 0.01  # never all zeros
        norm = sum(x * x for x in v) ** 0.5
        out.append([x / norm for x in v])
    return out


DOCS = {
    "committee_session:1": (
        "committee_session",
        7,
        [
            ("header", None, None, "ועדת הכספים. נכחו: חברי הוועדה. תקציב"),
            ("turns", "דוד ביטן", 1, "בוקר טוב, נפתח את הדיון."),
            ("turns", "אלון שוסטר", 2, "אני מבקש להעביר כסף לבתי הספר."),
            ("turns", "דוד ביטן", 1, "הדיון בתקציב המדינה יימשך מחר."),
            ("turns", "רונית לוי", None, "הרכבת לירושלים מתעכבת שוב."),
            ("turns", "דוד ביטן", 1, "תודה, הישיבה נעולה."),
        ],
    ),
    "bill:2": ("bill", None, [("paragraphs", None, None, "הצעת חוק התקציב לשנת 2025.")]),
}


@pytest.fixture
def chunks(db):
    with connect() as conn:
        for doc_id, (ptype, committee, rows) in DOCS.items():
            conn.execute(
                """insert into text.documents (doc_id, source_table, parent_type, url, status,
                       knesset_num, committee_id, doc_date)
                   values (%s, 'x', %s, %s, 'done', 25, %s, '2025-01-05')""",
                (doc_id, ptype, f"https://fs.knesset.gov.il/25\\x\\{doc_id}.docx", committee),
            )
            for ord_, (kind, speaker, pid, text) in enumerate(rows):
                vec = "[" + ",".join(f"{x:.5f}" for x in encode([text])[0]) + "]"
                cid = conn.execute(
                    """insert into text.chunks (doc_id, ord, text, n_tokens, chunk_hash,
                           speaker_raw, person_id, meta, embedding)
                       values (%s, %s, %s, 10, %s, %s, %s, jsonb_build_object('kind', %s::text),
                               %s::halfvec) returning chunk_id""",
                    (
                        doc_id,
                        ord_,
                        text,
                        hashlib.md5(text.encode()).hexdigest(),
                        speaker,
                        pid,
                        kind,
                        vec,
                    ),
                ).fetchone()[0]
                if pid == 2:
                    conn.execute(
                        """insert into text.chunk_entities (chunk_id, type, surface, ref_id,
                               source) values (%s, 'faction', 'יש עתיד', 30, 'speaker')""",
                        (cid,),
                    )
                if "ירושלים" in text:
                    conn.execute(
                        """insert into text.chunk_entities (chunk_id, type, surface, normalized,
                               source) values (%s, 'place', 'לירושלים', 'ירושלים', 'ner')""",
                        (cid,),
                    )


def texts(windows):
    return [[(c.mark, c.text[:12]) for c in w.chunks] for w in windows]


def test_fuse():
    assert fuse([1, 2], [2, 3]) == pytest.approx({1: 1 / 61, 2: 1 / 62 + 1 / 61, 3: 1 / 62})


def test_keyword_search_with_neighbors(chunks):
    windows = search("בתקציב", Filters(doc_types=["committee_session"]), encode=None)
    assert len(windows) == 1  # the header also says תקציב, but headers are excluded
    w = windows[0]
    assert w.url == "https://fs.knesset.gov.il/25/x/committee_session:1.docx"
    assert [(c.ord, c.mark) for c in w.chunks] == [(2, "before"), (3, "hit"), (4, "after")]
    # a bill gets no neighbors by default; neighbors=0 turns them off for protocols too
    assert [len(w.chunks) for w in search("תקציב", encode=None, neighbors=0)] == [1, 1]


def test_vector_search_finds_meaning(chunks):
    """No chunk contains the word תחבורה: only the vector finds the train."""
    assert search("תחבורה", encode=None) == []
    (w,) = search("תחבורה", k=1, neighbors=0, encode=encode)
    assert texts([w]) == [[("hit", "הרכבת לירושל")]]


def test_overlapping_windows_merge(chunks):
    (w,) = search("כסף תקציב", Filters(doc_types=["committee_session"]), k=2, encode=encode)
    assert [(c.ord, c.mark) for c in w.chunks] == [
        (1, "before"),
        (2, "hit"),
        (3, "hit"),
        (4, "after"),
    ]


def test_filters(chunks):
    def hits(**f):
        return sorted(
            c.chunk_id
            for w in search("תקציב כסף רכבת", Filters(**f), neighbors=0, encode=encode)
            for c in w.chunks
        )

    def ids(**where):
        with connect() as conn:
            sql = " and ".join(f"{k} = %({k})s" for k in where)
            return sorted(
                r[0] for r in conn.execute(f"select chunk_id from text.chunks where {sql}", where)
            )

    assert hits(person_ids=[2]) == ids(person_id=2)
    assert hits(faction_ids=[30]) == ids(person_id=2)
    assert hits(entity="ירושלים") == ids(speaker_raw="רונית לוי")
    assert hits(doc_types=["bill"]) == ids(doc_id="bill:2")
    assert hits(knessets=[24]) == []
    assert hits(committee_ids=[7], date_to="2025-01-04") == []
