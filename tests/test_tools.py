import json

import pytest
from test_search import chunks, encode  # noqa: F401  (the search fixture: 3 documents)

from knesset import search as search_module
from knesset.db import connect
from knesset.tools.common import resolve_entity, truncate
from knesset.tools.research import facets, get_chunks, get_document, search_text, share_of_words
from knesset.tools.sql import check_select, describe_schema, make_chart, sql_query
from knesset.tools.summary import get_document_summary
from knesset.tools.verify import rerun_sql, verdict

MARTS = """
create table marts.dim_person as
    select 1::bigint person_id, 'דוד ביטן'::text full_name, array['דוד ביטן'] name_variants,
           array[24, 25] mk_knessets, 25 last_knesset
    union all select 2, 'אלון שוסטר', array['אלון שוסטר'], array[25], 25
    union all select 3, 'דוד אמסלם', array['דוד אמסלם'], array[25], 25;
create table marts.dim_faction as
    select 30::bigint faction_id, 'יש עתיד'::text faction_name, 25 knesset_num;
create table marts.dim_committee as
    select 7::bigint committee_id, 'ועדת הכספים'::text committee_name, 25 knesset_num
    union all select 8, 'ועדת הכספים', 24;
create table marts.dim_ministry as select 5::bigint ministry_id, 'משרד האוצר'::text ministry_name;
create table marts.fct_bill as
    select 900::bigint bill_id, 'חוק התקציב לשנת 2025'::text bill_name, 25 knesset_num;
create table marts.fct_person_position as
    select 2::bigint person_id, 30::bigint faction_id, 'יש עתיד'::text faction_name,
           '2022-11-15'::timestamp start_date, null::timestamp finish_date;
"""


@pytest.fixture
def data(chunks, monkeypatch):  # noqa: F811
    monkeypatch.setattr(search_module, "default_encoder", lambda: encode)
    # never load the real cross-encoder in tests: keep the fused order
    monkeypatch.setattr(search_module, "default_reranker",
                        lambda: lambda q, texts: [-i for i in range(len(texts))])  # fmt: skip
    with connect() as conn:
        conn.execute(MARTS)
        conn.execute("""insert into text.sections values
            ('committee_session:1', 0, 'נושא', 'תקציב המדינה', 2, 900, 30, 3),
            ('committee_session:1', 1, 'נושא', 'הרכבת', null, null, 10, 2)""")
        conn.execute("""insert into text.section_speakers values
            ('committee_session:1', 0, 'דוד ביטן', 'יור', 1, 20, 2),
            ('committee_session:1', 0, 'אלון שוסטר', 'דובר', 2, 10, 1),
            ('committee_session:1', 1, 'רונית לוי', 'אורח', null, 10, 2)""")
        conn.execute("""insert into text.participants values
            ('committee_session:1', 0, 'רונית לוי', 'מוזמנים', 'עו"ד, משרד האוצר',
             'משרד האוצר', null)""")


def json_ok(result: dict) -> dict:
    assert "evidence" in result
    json.dumps(result)  # every tool returns JSON
    return result


def test_truncate():
    assert truncate("אחת שתיים שלוש", 9) == "אחת …"
    assert truncate("קצר", 9) == "קצר"


def test_resolve_entity(data):
    found = json_ok(resolve_entity("ועדת הכספים", knesset=25))["candidates"]
    assert found == [
        {"type": "committee", "id": 7, "name": "ועדת הכספים", "knesset": 25, "score": 1.0}
    ]
    people = resolve_entity("דוד", types=["person"])["candidates"]
    assert {p["id"] for p in people} == {1, 3}
    assert resolve_entity("אוצר")["candidates"][0]["type"] == "ministry"
    assert resolve_entity("חוק התקציב")["candidates"][0]["id"] == 900


def test_search_text_and_get_chunks(data):
    result = json_ok(search_text("רכבת", {"committee_ids": [7]}, k=1))
    (hit,) = result["results"]
    assert hit["url"].startswith("https://") and hit["same_text_in"] == 0
    assert [c["mark"] for c in hit["chunks"]] == ["before", "hit", "after"]
    assert result["evidence"] == [
        {"type": "chunk", "ref": hit["chunks"][1]["chunk_id"], "url": hit["url"]}
    ]
    with pytest.raises(ValueError, match="unknown filters"):
        search_text("רכבת", {"committee": 7})
    again = get_chunks([hit["chunks"][1]["chunk_id"]], neighbors=0)
    assert [c["text"] for c in again["results"][0]["chunks"]] == ["הרכבת לירושלים מתעכבת שוב."]


def test_get_document(data):
    doc = json_ok(get_document("committee_session:1", window=1))
    assert doc["document"]["n_chunks"] == 6 and "\\" not in doc["document"]["url"]
    assert [c["ord"] for c in doc["chunks"]] == [0, 1, 2]
    middle = get_document(
        "committee_session:1", around_chunk=doc["chunks"][2]["chunk_id"], window=1
    )
    assert [c["ord"] for c in middle["chunks"]] == [1, 2, 3]


def test_facets(data):
    by_speaker = json_ok(facets("speaker", {"doc_types": ["committee_session"]}))["counts"]
    assert by_speaker[0] == {"value": "דוד ביטן", "chunks": 3, "documents": 1}
    by_committee = facets("committee", query="רכבת")["counts"]
    assert {r["value"] for r in by_committee} == {"ועדת הכספים"}
    assert facets("entity")["counts"] == [{"value": "place: ירושלים", "chunks": 2, "documents": 2}]


def test_share_of_words(data):
    by_section = json_ok(share_of_words("section", {"committee_ids": [7]}))
    assert by_section["total_words"] == 40
    assert [(r["value"], r["pct"]) for r in by_section["shares"]] == [
        ("תקציב המדינה", 75.0),
        ("הרכבת", 25.0),
    ]
    by_faction = share_of_words("faction")["shares"]
    assert {r["value"]: r["words"] for r in by_faction} == {
        "(לא חברי כנסת / לא זוהו)": 10,
        "(ללא סיעה)": 20,
        "יש עתיד": 10,
    }
    assert share_of_words("speaker", {"items": [[2, 900]]})["total_words"] == 30


def test_document_summary(data):
    s = json_ok(get_document_summary("committee_session:1"))
    assert [x["pct"] for x in s["sections"]] == [75.0, 25.0]
    assert s["top_speakers"][0]["speaker"] == "דוד ביטן"
    assert s["organizations"] == [{"organization": "משרד האוצר", "people": 1}]
    assert s["top_entities"] == [{"type": "place", "name": "ירושלים", "mentions": 1}]


def test_sql_tools(data):
    assert check_select("select 1;") == "select 1"
    for bad in ("delete from marts.dim_person", "select 1; drop table x"):
        with pytest.raises(ValueError):
            check_select(bad)
    result = json_ok(sql_query("select person_id, full_name from marts.dim_person order by 1"))
    assert result["columns"] == ["person_id", "full_name"] and result["row_count"] == 3
    assert result["evidence"] == [
        {"type": "sql", "ref": "select person_id, full_name from marts.dim_person order by 1"}
    ]
    big = sql_query("select g from generate_series(1, 500) g")
    assert big["row_count"] == 200 and big["truncated"]
    assert rerun_sql("select 1 as x")["rows"] == [[1]]
    with pytest.raises(Exception, match="read-only"):  # agent_ro's transactions are read-only
        sql_query("select * from marts.dim_person for update")


def test_describe_schema():
    tables = describe_schema()["tables"]
    assert {"table": "marts.fct_vote"} | tables[
        [t["table"] for t in tables].index("marts.fct_vote")
    ]
    (vote,) = [
        t for t in describe_schema("fct_vote_result")["tables"] if "vote_result" in t["table"]
    ]
    assert "result_desc" in vote["columns"]


def test_make_chart():
    data = {
        "columns": ["year", "n", "party"],
        "rows": [[2024, 3, "א"], [2025, 5, "א"], [2025, 2, "ב"]],
    }
    chart = json_ok(make_chart(**data, kind="line", x="year", y="n", series="party"))
    assert [t["name"] for t in chart["chart"]["data"]] == ["א", "ב"]
    assert chart["chart"]["data"][0]["y"] == [3, 5]
    with pytest.raises(ValueError, match="no column"):
        make_chart(**data, kind="bar", x="month", y="n")


def test_tool_schemas_have_no_open_objects():
    """OpenAI rejects a required free-form object argument (make_chart had two)."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from knesset.tools.lc import AGENT_TOOLS, as_tool

    for fn in {f for tools in AGENT_TOOLS.values() for f in tools}:
        params = convert_to_openai_tool(as_tool(fn))["function"]["parameters"]
        for name in params.get("required", []):  # a required argument must not be an open map
            assert "additionalProperties" not in params["properties"][name], f"{fn.__name__}.{name}"


def test_verdict():
    assert verdict("pass") == {"status": "pass", "issues": [], "evidence": []}
    with pytest.raises(ValueError):
        verdict("fail")
    assert verdict("fail", ["number of votes is 12, not 21"])["issues"]


def test_langchain_tools(data):
    from knesset.tools.lc import AGENT_TOOLS, tools_for

    assert set(AGENT_TOOLS) == {"orchestrator", "analyst", "researcher", "summarizer", "verifier"}
    (sql,) = [t for t in tools_for("analyst") if t.name == "sql_query"]
    assert "read-only" in sql.description and set(sql.args) == {"sql"}
    out = json.loads(sql.invoke({"sql": "select 1 as x"}))
    assert out["rows"] == [[1]]
    assert "only SELECT" in sql.invoke({"sql": "drop table x"})  # errors go back to the agent
    assert "does not exist" in sql.invoke({"sql": "select * from marts.nope"})
    (search,) = [t for t in tools_for("researcher") if t.name == "search_text"]
    assert set(search.args) == {"query", "filters", "k", "neighbors"}


def test_counts_cite_rerunnable_sql(data):
    """facets and share_of_words cite the SQL they ran; re-running it gives the same numbers."""
    f = facets("speaker", {"doc_types": ["committee_session"]}, query="תקציב")
    (ev,) = f["evidence"]
    assert ev["type"] == "sql" and "%(" not in ev["ref"]
    again = rerun_sql(ev["ref"])
    assert [r[0] for r in again["rows"]] == [r["value"] for r in f["counts"]]
    s = share_of_words("faction", {"committee_ids": [7]})
    rows = rerun_sql(s["evidence"][0]["ref"])["rows"]
    assert {r[0]: r[1] for r in rows} == {r["value"]: r["words"] for r in s["shares"]}
