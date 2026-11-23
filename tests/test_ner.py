import re

from knesset.db import connect
from knesset.enrich.ner import candidates, ner_text, run_ner

NAMES = {
    "בנימין נתניהו": "PER",
    "בליכוד": "ORG",
    "משרד האוצר": "ORG",
    "בירושלים": "GPE",
    "ירושלים": "GPE",
    "סמוטריץ": "PER",
    "כץ": "PER",
    "בכנסת": "ORG",
    "הכנסת": "ORG",
}
PEOPLE = """insert into marts.dim_person values
    (1, 'בנימין נתניהו', array['בנימין נתניהו'], 'בנימין', 'נתניהו'),
    (2, 'בצלאל סמוטריץ''', array['בצלאל סמוטריץ'''], 'בצלאל', 'סמוטריץ'''),
    (3, 'ישראל כץ', array['ישראל כץ'], 'ישראל', 'כץ'),
    (4, 'חיים כץ', array['חיים כץ'], 'חיים', 'כץ')"""


def fake_tagger(texts):
    """Stands in for the model: finds the planted names."""
    return [
        [
            {"entity_group": label, "score": 0.99, "start": m.start(), "end": m.end()}
            for name, label in NAMES.items()
            for m in re.finditer(rf"(?<!\w){name}", text)  # whole words, as the model
        ]
        for text in texts
    ]


def test_ner_text_and_candidates():
    assert ner_text('<< יור >> היו"ר דוד ביטן: << יור >>\nבוקר טוב.') == "בוקר טוב."
    assert candidates("בליכוד") == ["בליכוד", "הליכוד", "ליכוד"]
    assert candidates("והליכוד") == ["והליכוד", "הליכוד"]  # the article stays
    assert candidates("הכנסת") == ["הכנסת"]
    assert candidates("משה") == ["משה", "שה"]


def test_run_ner(db):
    with connect() as conn:
        conn.execute("""create table marts.dim_person (person_id bigint, full_name text,
                            name_variants text[], first_name text, last_name text)""")
        conn.execute(PEOPLE)
        conn.execute("""create table marts.fct_person_position as
            select p::bigint person_id, 10::bigint faction_id, 'הליכוד'::text faction_name,
                   '2022-11-15'::timestamp start_date, null::timestamp finish_date
            from generate_series(1, 4) p""")
        conn.execute("""insert into text.documents (doc_id, source_table, parent_type, url,
                            status, knesset_num, doc_date)
                        values ('d:1', 'x', 'committee_session', 'u', 'done', 25, '2024-01-01')""")
        for ord_, kind, text in [
            (0, "header", "ירושלים"),
            (1, "turns", "<< דובר >> בנימין נתניהו: << דובר >>\nבליכוד, משרד האוצר, בירושלים."),
            (2, "turns", "ראש הממשלה בנימין נתניהו. ירושלים"),
            (3, "turns", "<< סיום >> הישיבה ננעלה. << סיום >>"),
            (4, "turns", "סמוטריץ, כץ דיברו בכנסת. הכנסת"),
        ]:
            conn.execute(
                """insert into text.chunks (doc_id, ord, text, n_tokens, chunk_hash, meta)
                   values ('d:1', %s, %s, 10, md5(%s), jsonb_build_object('kind', %s::text))""",
                (ord_, text, text, kind),
            )
    done = {"chunks": 4, "entities": 9, "linked": 3, "prefixes_merged": 2}
    assert run_ner([25], tagger=fake_tagger) == done
    assert run_ner([25], tagger=fake_tagger) == dict.fromkeys(done, 0)
    with connect() as conn:
        rows = conn.execute(
            """select c.ord, e.type, e.surface, e.normalized, e.ref_table, e.ref_id
               from text.chunk_entities e join text.chunks c using (chunk_id)
               where e.source = 'ner' order by 1, 3"""
        ).fetchall()
    # the speaker line is not read; the header chunk is skipped
    assert rows == [
        (1, "place", "בירושלים", "ירושלים", None, None),  # the prefix form merged
        (1, "org", "בליכוד", "הליכוד", "dim_faction", 10),
        (1, "org", "משרד האוצר", "משרד האוצר", None, None),
        (2, "person", "בנימין נתניהו", "בנימין נתניהו", "dim_person", 1),
        (2, "place", "ירושלים", "ירושלים", None, None),
        (4, "org", "בכנסת", "הכנסת", None, None),  # the article kept
        (4, "org", "הכנסת", "הכנסת", None, None),
        (4, "person", "כץ", "כץ", None, None),  # two people named כץ: not linked
        (4, "person", "סמוטריץ", "בצלאל סמוטריץ'", "dim_person", 2),  # last name, geresh
    ]
