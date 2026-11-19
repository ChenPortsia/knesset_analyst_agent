from knesset.db import connect
from knesset.enrich import links as ln

BILL, QUERY = (2, 9000), (1, 77)
ITEMS = [(*BILL, ln.words("חוק הבדיקה, התשפ״ו-2026")), (*QUERY, ln.words("שאילתה על מחיר הדבש"))]


def test_topic_walk():
    chunks = [
        (1, "header", ["חוק הבדיקה, התשפ״ו-2026", "שאילתה על מחיר הדבש"]),
        (2, "turns", []),  # opening remarks, before any topic
        (3, "turns", ["1. חוק הבדיקה"]),
        (4, "turns", []),
        (5, "turns", ["שאילתה על מחיר הדבש"]),  # the bill ends, the query starts
        (6, "turns", []),
        (7, "turns", ["נושא שאינו בסדר היום"]),  # unmatched: ends the query
        (8, "turns", []),
    ]
    assert ln.link_chunks(chunks, ITEMS) == [
        (1, *QUERY),
        (1, *BILL),
        (3, *BILL),
        (4, *BILL),
        (5, *QUERY),
        (5, *BILL),
        (6, *QUERY),
        (7, *QUERY),
    ]


def test_single_item_session_links_everything():
    chunks = [(1, "header", []), (2, "turns", []), (3, "turns", ["נושא כלשהו"])]
    assert ln.link_chunks(chunks, ITEMS[:1]) == [(1, *BILL), (2, *BILL), (3, *BILL)]


def test_best_item_needs_overlap():
    assert ln.best_item("הצעת חוק הבדיקה", ITEMS) == BILL
    assert ln.best_item("חוק אחר", ITEMS) is None


def q(sql: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(sql).fetchall()


def test_link_end_to_end(db):
    with connect() as conn:
        conn.execute("""create table marts.fct_session_item as
            select 'committee'::text session_type, 500::bigint session_id, 2 item_type_id,
                   9000::bigint item_id, 'חוק הבדיקה'::text item_name
            union all select 'committee', 500, 1, 77, 'שאילתה על מחיר הדבש'""")
        conn.execute("""create table marts.dim_person as
            select 1::bigint person_id, 'דוד ביטן'::text full_name""")
        conn.execute("""create table marts.fct_person_position as
            select 1::bigint person_id, 100::bigint faction_id, 'סיעה א'::text faction_name,
                   '2022-11-15'::timestamp start_date, '2024-06-30'::timestamp finish_date
            union all select 1, 101, 'סיעה ב', '2024-07-01', null""")
        conn.execute("""insert into text.documents (doc_id, source_table, parent_type,
                            parent_id, url, status, knesset_num, doc_date)
                        values ('committee_session:1', 'x', 'committee_session', 500, 'u',
                                'done', 25, '2025-01-05 10:00')""")
        texts = [
            (
                "header",
                "סדר היום:\n<< נושא >> חוק הבדיקה << נושא >>\n"
                "<< נושא >> שאילתה על מחיר הדבש << נושא >>",
                None,
                None,
            ),
            (
                "turns",
                "<< נושא >> חוק הבדיקה << נושא >>\n<< יור >> דוד ביטן: << יור >>\nבוקר טוב.",
                "דוד ביטן",
                1,
            ),
            ("turns", "<< נושא >> שאילתה על מחיר הדבש << נושא >>\nתשובה.", None, None),
        ]
        for ord_, (kind, text, speaker, pid) in enumerate(texts):
            conn.execute(
                """insert into text.chunks (doc_id, ord, text, n_tokens, chunk_hash, speaker_raw,
                       person_id, speaker_confidence, meta)
                   values ('committee_session:1', %s, %s, 10, md5(%s), %s, %s, %s,
                           jsonb_build_object('kind', %s::text))""",
                (ord_, text, text, speaker, pid, 1.0 if pid else None, kind),
            )

    expected = {"chunk_items": 5, "person": 1, "faction": 1}
    assert ln.link([25]) == expected
    assert ln.link([25]) == expected  # idempotent
    rows = q("""select c.ord, ci.item_id from text.chunk_items ci join text.chunks c
                using (chunk_id) order by 1, 2""")
    assert rows == [(0, 77), (0, 9000), (1, 9000), (2, 77), (2, 9000)]
    # the faction on the session date (after the switch)
    assert q("select type, normalized, ref_id from text.chunk_entities order by type") == [
        ("faction", "סיעה ב", 101),
        ("person", "דוד ביטן", 1),
    ]
