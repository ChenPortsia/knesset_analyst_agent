"""The semantic layer: true to the schema, compiled metrics that run, and the new tools."""

import re
from pathlib import Path

import pytest
import yaml
from test_search import chunks, encode  # noqa: F401

from knesset import semantic
from knesset.db import connect
from knesset.tools.metrics import bill_votes, compute, metric, top_participants
from knesset.tools.sql import sql_query

ROOT = Path(__file__).resolve().parents[1]
MARTS = """
create table marts.dim_person as select * from (values
    (1::bigint, 'דוד ביטן'::text, 'ביטן'::text), (2, 'אלון שוסטר', 'שוסטר')) as t
    (person_id, full_name, last_name);
create table marts.dim_committee as
    select 7::bigint committee_id, 'ועדת הכספים'::text committee_name;
create table marts.fct_bill as select * from (values
    (900::bigint, 25, 'חוק א'::text, 'פרטית'::text, 'התקבלה'::text, true, '2025-03-01'::timestamp,
     7::bigint, array[1, 2]::bigint[]),
    (901, 25, 'חוק ב', 'פרטית', 'התקבלה', true, '2025-04-01', 7, array[1]::bigint[]),
    (902, 25, 'חוק ג', 'ממשלתית', 'בוועדה', false, null, 7, array[]::bigint[])) as t
    (bill_id, knesset_num, bill_name, subtype_desc, status_desc, is_passed, publication_date,
     committee_id, initiator_ids);
create table marts.fct_committee_session as select * from (values
    (1::bigint, 25, 7::bigint, 'ועדת הכספים'::text, 'פעילה'::text, 'פתוחה'::text,
     '2025-01-05'::timestamp)) as t
    (committee_session_id, knesset_num, committee_id, committee_name, status_desc, type_desc,
     start_date);
create table marts.fct_plenum_session as
    select 50::bigint plenum_session_id, 25 knesset_num, '2025-03-24'::timestamp start_date;
create table marts.fct_vote as select * from (values
    (1::bigint, '2024-12-16 10:00'::timestamp, 50::bigint, 900::bigint, 1,
     'הצעת חוק א - קריאה ראשונה'::text, ''::text, 'התקבלה'::text, 59, 57, 0),
    (2, '2025-03-24 10:00', 50, 900, 2, 'סעיף 1', 'הסתייגות', 'נדחתה', 52, 66, 0),
    (3, '2025-03-24 11:00', 50, 900, 3, 'הצעת חוק א - קריאה שלישית', '', 'התקבלה', 66, 52, 0))
    as t (vote_id, vote_datetime, plenum_session_id, item_id, ordinal, vote_title,
          vote_subject, vote_status_desc, n_for, n_against, n_abstain);
create table marts.fct_vote_result as select * from (values
    (1::bigint, 3::bigint, 1::bigint, 'בעד'::text, 30::bigint, 'הליכוד'::text, 900::bigint,
     '2025-03-24 11:00'::timestamp, 50::bigint),
    (2, 3, 2, 'נגד', 31, 'יש עתיד', 900, '2025-03-24 11:00', 50)) as t
    (vote_result_id, vote_id, person_id, result_desc, faction_id, faction_name, item_id,
     vote_datetime, plenum_session_id);
create table marts.fct_query as select * from (values
    (1::bigint, 25, 1::bigint, 5::bigint, 'משרד החינוך'::text, 'רגילה'::text, 'נענתה'::text,
     '2025-02-01'::timestamp)) as t
    (query_id, knesset_num, person_id, ministry_id, ministry_name, type_desc, status_desc,
     submit_date);
"""


@pytest.fixture
def marts(chunks):  # noqa: F811
    with connect() as conn:
        conn.execute(MARTS)
        conn.execute("""insert into text.sections values
            ('committee_session:1', 0, 'נושא', 'חוק א', 2, 900, 30, 3)""")
        conn.execute("""insert into text.section_speakers values
            ('committee_session:1', 0, 'דוד ביטן', 'יור', 1, 20, 2),
            ('committee_session:1', 0, 'רונית לוי', 'אורח', null, 10, 1)""")
        conn.execute("""insert into text.participants values
            ('committee_session:1', 0, 'רונית לוי', 'מוזמנים', 'עו"ד, משרד האוצר', 'משרד האוצר',
             null),
            ('committee_session:1', 1, 'דני כהן', 'מוזמנים', 'ההסתדרות', 'ההסתדרות', null),
            ('committee_session:3', 0, 'רונית לוי', 'מוזמנים', 'משרד האוצר', 'משרד האוצר', null)""")


def test_layer_columns_exist_in_the_schema():
    """Every alias.column the metrics use is a column of its table (dbt docs, migrations)."""
    marts = {m["name"]: {c["name"] for c in m.get("columns", [])} for m in yaml.safe_load(
        (ROOT / "dbt/models/marts/schema.yml").read_text())["models"]}  # fmt: skip
    ddl = " ".join(p.read_text() for p in (ROOT / "sql").glob("*.sql"))
    for name, m in semantic.layer()["metrics"].items():
        text = " ".join([m["from"], m["value"], m.get("where") or "", m.get("date") or ""]
                        + [str(v) for v in semantic.dims(name).values()])  # fmt: skip
        alias = {a: (s, t) for s, t, a in re.findall(r"\b(marts|text)\.(\w+) (?:as )?(\w+)", text)}
        for a, col in re.findall(r"\b([a-z]{1,2})\.([a-z_]+)\b", text):
            if a not in alias:
                continue
            schema, table = alias[a]
            ok = col in marts.get(table, ()) if schema == "marts" else f" {col} " in ddl
            assert ok, f"{name}: {a}.{col} is not a column of {schema}.{table}"


def test_every_metric_and_dimension_runs(marts):
    for name in semantic.layer()["metrics"]:
        for d in semantic.dims(name):
            result = metric(name, by=[d], limit=5)
            assert result["evidence"][0]["type"] == "sql"
            sql_query(result["evidence"][0]["ref"])  # the cited SQL re-runs as is


def test_metric_values_and_filters(marts):
    rows = metric("bills_passed", ["initiator"], {"knesset": [25], "subtype": ["פרטית"]})["rows"]
    assert rows == [{"initiator": "דוד ביטן", "bills_passed": 2},
                    {"initiator": "אלון שוסטר", "bills_passed": 1}]  # fmt: skip
    monthly = metric("bills_passed", ["month"], {"date_from": "2025-03-15"})["rows"]
    assert monthly == [{"month": "2025-04", "bills_passed": 1}]
    assert metric("words_spoken", ["speaker"])["rows"][0] == {"speaker": "דוד ביטן",
                                                             "words_spoken": 20}  # fmt: skip
    with pytest.raises(ValueError, match="dimensions"):
        metric("bills", ["ministry"])
    with pytest.raises(ValueError, match="unknown metric"):
        metric("laws")


def test_bill_votes_finds_the_final_vote(marts):
    result = bill_votes(900)
    assert result["n_votes"] == 3 and result["second_reading_votes"]["count"] == 1
    assert [v["stage"] for v in result["votes"]] == ["first", "final"]
    final = result["final_vote"]
    assert (final["vote_id"], final["n_for"], final["n_against"]) == (3, 66, 52)
    assert {r["faction_name"] for r in final["by_faction"]} == {"הליכוד", "יש עתיד"}
    assert len(result["evidence"]) == 2


def test_top_participants_counts_sessions(marts):
    rows = top_participants("organization", {"knesset": [25]})["rows"]
    assert rows[0] == {"organization": "משרד האוצר", "attendance": 2}  # in 2 protocols
    committee = top_participants("organization", {"committee": [7]})["rows"]
    assert {r["organization"]: r["attendance"] for r in committee} == {"משרד האוצר": 1,
                                                                      "ההסתדרות": 1}  # fmt: skip
    with pytest.raises(ValueError):
        top_participants("speaker")


def test_compute_is_safe_and_citable():
    result = compute("100 * 371,397 / 7940859")
    assert result["result"] == pytest.approx(4.677, abs=1e-4)
    assert result["evidence"][0] == {"type": "calc", "ref": "100 * 371,397 / 7940859 = 4.677"}
    for bad in ("__import__('os')", "total / 2", "2 ** 3", "1 +"):
        with pytest.raises(ValueError):
            compute(bad)


def test_sql_errors_list_the_real_columns(marts):
    with pytest.raises(ValueError) as e:
        sql_query("select knesset_num from marts.fct_vote")
    msg = str(e.value)
    assert "marts.fct_vote: vote_id, vote_datetime" in msg
    assert "join `marts.fct_plenum_session using (plenum_session_id)`" in msg  # layer note
    with pytest.raises(ValueError, match="Did you mean: subtype_desc"):
        sql_query("select sub_type_desc from marts.fct_bill")


def test_briefs_give_each_agent_its_part():
    analyst, orchestrator = semantic.brief("analyst"), semantic.brief("orchestrator")
    assert "`bills_passed`" in analyst and "marts.fct_vote" in analyst
    assert "top_participants" in orchestrator and "marts.fct_vote " not in orchestrator
    assert "Tables" in semantic.brief("researcher") and "text.participants" in semantic.brief(
        "researcher"
    )
    assert len(analyst) < 8000 and len(orchestrator) < 4000  # prompt size stays small


def test_metric_filters_accept_search_style_names(marts):
    """Agents write search_text's filter names; they mean the same dimensions."""
    a = metric("attendance", ["organization"], {"committee_ids": [7], "knessets": [25]})
    b = metric("attendance", ["organization"], {"committee": [7], "knesset": [25]})
    assert a["rows"] == b["rows"] and a["rows"]
    assert metric("bills", ["status"], {"committee_id": 7})["rows"]  # 'status' stays itself


def test_unknown_search_filters_say_what_to_use_instead():
    from knesset.tools.research import to_filters

    with pytest.raises(ValueError, match="facets"):
        to_filters({"query": "פסולת"})
    with pytest.raises(ValueError, match="doc_types"):
        to_filters({"parent_type": "plenum_session"})
