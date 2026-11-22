from knesset.db import connect
from knesset.enrich.structure import build, parse_participants, split_sections

WORDS = " ".join(["מילה"] * 30)  # one long enough answer
PROTOCOL = f"""פרוטוקול מס' 139
סדר היום:
<< הצח >> חוק הבדיקה << הצח >>
נכחו:
חברי הוועדה:
דוד ביטן – היו"ר
אלון שוסטר
מוזמנים:
רונית לוי | – | עו"ד, לשכה משפטית, משרד הפנים
יוסי כהן | – | משפחה מאמצת
רישום פרלמנטרי:
א.ב., חבר תרגומים
רשימת הנוכחים על תואריהם מבוססת על המידע שהוזן במערכת.
<< הצח >> חוק הבדיקה
הכנה לקריאה שנייה << הצח >>
<< יור >> היו"ר דוד ביטן: << יור >>
בוקר טוב, אני פותח את הישיבה. {WORDS}
<< אורח >> רונית לוי: << אורח >>
{WORDS}
<< יור >> היו"ר דוד ביטן: << יור >>
תודה. עוברים לנושא הבא.
<< הצח >> שאילתה על מחיר הדבש << הצח >>
<< נושא >> שאילתה על מחיר הדבש, של ח"כ אלון שוסטר << נושא >>
הדבש יקר מדי. {WORDS}
<< דובר >> אלון שוסטר: << דובר >>
{WORDS}
<< דובר_המשך >> אלון שוסטר: << דובר_המשך >>
{WORDS}
<< סיום >> הישיבה ננעלה בשעה 10:20. << סיום >>"""


def test_parse_participants():
    rows = [(p.name, p.grp, p.description, p.organization) for p in parse_participants(PROTOCOL)]
    assert rows == [
        ("דוד ביטן", "חברי הוועדה", 'היו"ר', None),
        ("אלון שוסטר", "חברי הוועדה", None, None),
        ("רונית לוי", "מוזמנים", 'עו"ד, לשכה משפטית, משרד הפנים', "משרד הפנים"),
        ("יוסי כהן", "מוזמנים", "משפחה מאמצת", None),
    ]  # the transcription service is not a participant


def test_split_sections():
    sections = split_sections(PROTOCOL)
    assert [(s.tag, s.title) for s in sections] == [
        ("הצח", "חוק הבדיקה הכנה לקריאה שנייה"),
        ("נושא", 'שאילתה על מחיר הדבש, של ח"כ אלון שוסטר'),  # נושא wins over הצח
    ]
    first, second = sections
    assert first.speakers == {
        ('היו"ר דוד ביטן', "יור"): [40, 2],
        ("רונית לוי", "אורח"): [30, 1],
    }
    # words after a marker belong to whoever spoke last; the closing line is not speech
    assert second.speakers == {
        ('היו"ר דוד ביטן', "יור"): [33, 0],
        ("אלון שוסטר", "דובר"): [30, 1],
        ("אלון שוסטר", "דובר_המשך"): [30, 1],
    }


def test_build(db):
    with connect() as conn:
        conn.execute("""create table marts.dim_person as
            select 1::bigint person_id, array['דוד ביטן'] name_variants, 'דוד' first_name,
                   'ביטן' last_name
            union all select 2, array['אלון שוסטר'], 'אלון', 'שוסטר'""")
        conn.execute("""create table marts.fct_person_position as
            select 1::bigint person_id, '2022-11-15'::timestamp start_date,
                   null::timestamp finish_date union all select 2, '2022-11-15', null""")
        conn.execute("""create table marts.fct_session_item as
            select 'committee'::text session_type, 500::bigint session_id, 2 item_type_id,
                   9000::bigint item_id, 'חוק הבדיקה'::text item_name
            union all select 'committee', 500, 1, 77, 'שאילתה על מחיר הדבש'""")
        conn.execute(
            """insert into text.documents (doc_id, source_table, parent_type, parent_id, url,
                   status, knesset_num, doc_date, group_type, text, content_hash)
               values ('committee_session:1', 'x', 'committee_session', 500, 'u', 'done', 25,
                       '2024-10-29', 'פרוטוקול ועדה', %s, 'h')""",
            (PROTOCOL,),
        )
    expected = {"protocols": 1, "sections": 2, "section_speakers": 4, "participants": 4}
    assert build([25]) == expected
    assert build([25]) == expected  # rebuilt, not appended
    with connect() as conn:
        assert conn.execute(
            "select ord, item_id, n_words, n_turns from text.sections order by ord"
        ).fetchall() == [(0, 9000, 70, 3), (1, 77, 93, 2)]
        assert conn.execute(
            "select speaker_raw, person_id from text.section_speakers where ord = 1 order by 1"
        ).fetchall() == [("אלון שוסטר", 2), ('היו"ר דוד ביטן', 1)]
        assert conn.execute(
            "select name, person_id from text.participants order by ord"
        ).fetchall() == [
            ("דוד ביטן", 1),
            ("אלון שוסטר", 2),
            ("רונית לוי", None),
            ("יוסי כהן", None),
        ]
