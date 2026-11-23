import re

from knesset.db import connect
from knesset.enrich.ner import candidates, ner_text, run_ner

NAMES = {"בנימין נתניהו": "PER", "בליכוד": "ORG", "משרד האוצר": "ORG", "ירושלים": "GPE"}


def fake_tagger(texts):
    """Stands in for the model: finds the planted names."""
    return [
        [
            {"entity_group": label, "score": 0.99, "start": m.start(), "end": m.end()}
            for name, label in NAMES.items()
            for m in re.finditer(name, text)
        ]
        for text in texts
    ]


def test_ner_text_and_candidates():
    assert ner_text('<< יור >> היו"ר דוד ביטן: << יור >>\nבוקר טוב.') == "בוקר טוב."
    assert candidates("בליכוד") == ["בליכוד", "ליכוד", "הליכוד"]
    assert candidates("והליכוד") == ["והליכוד", "הליכוד", "ליכוד"]
    assert candidates("משה") == ["משה", "שה"]


def test_run_ner(db):
    with connect() as conn:
        conn.execute("""create table marts.dim_person as
            select 1::bigint person_id, 'בנימין נתניהו'::text full_name,
                   array['בנימין נתניהו'] name_variants, 'בנימין' first_name, 'נתניהו' last_name""")
        conn.execute("""create table marts.fct_person_position as
            select 1::bigint person_id, 10::bigint faction_id, 'הליכוד'::text faction_name,
                   '2022-11-15'::timestamp start_date, null::timestamp finish_date""")
        conn.execute("""insert into text.documents (doc_id, source_table, parent_type, url,
                            status, knesset_num, doc_date)
                        values ('d:1', 'x', 'committee_session', 'u', 'done', 25, '2024-01-01')""")
        for ord_, kind, text in [
            (0, "header", "ירושלים"),
            (1, "turns", "<< דובר >> בנימין נתניהו: << דובר >>\nבליכוד ובמשרד האוצר בירושלים."),
            (2, "turns", "ראש הממשלה בנימין נתניהו."),
            (3, "turns", "<< סיום >> הישיבה ננעלה. << סיום >>"),
        ]:
            conn.execute(
                """insert into text.chunks (doc_id, ord, text, n_tokens, chunk_hash, meta)
                   values ('d:1', %s, %s, 10, md5(%s), jsonb_build_object('kind', %s::text))""",
                (ord_, text, text, kind),
            )
    assert run_ner([25], tagger=fake_tagger) == {"chunks": 3, "entities": 4, "linked": 2}
    assert run_ner([25], tagger=fake_tagger) == {"chunks": 0, "entities": 0, "linked": 0}
    with connect() as conn:
        rows = conn.execute(
            """select c.ord, e.type, e.surface, e.normalized, e.ref_table, e.ref_id
               from text.chunk_entities e join text.chunks c using (chunk_id)
               where e.source = 'ner' order by 1, 3"""
        ).fetchall()
    # the speaker line is not read; the header chunk is skipped
    assert rows == [
        (1, "org", "בליכוד", "הליכוד", "dim_faction", 10),
        (1, "place", "ירושלים", "ירושלים", None, None),
        (1, "org", "משרד האוצר", "משרד האוצר", None, None),
        (2, "person", "בנימין נתניהו", "בנימין נתניהו", "dim_person", 1),
    ]
