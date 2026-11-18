import asyncio
import json

import psycopg
import pytest
from fake_odata import FakeOData, person, sample_tables

from knesset import sync as sync_mod
from knesset.config import settings
from knesset.db import connect
from knesset.odata import OData


def run_sync(fake: FakeOData, **kw) -> dict[str, int]:
    return asyncio.run(sync_mod.sync(api=OData(transport=fake.transport(), backoff_s=0), **kw))


def q(sql: str, params: tuple = ()) -> list[tuple]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def state(table: str) -> dict:
    return q("select state from ops.sync_state where table_name = %s", (table,))[0][0]


def test_full_load(db, monkeypatch):
    monkeypatch.setattr(sync_mod, "BIG_TABLE", 100)  # make KNS_Person (250 rows) partitioned
    fake = FakeOData(sample_tables(250))
    written = run_sync(fake)

    assert written == {
        "KNS_Person": 250,
        "KNS_ItemType": 5,
        "KNS_DocumentQuerie": 3,
        "V_Lobbyists": 3,
    }
    assert q("select count(*), count(distinct id) from raw.kns_person") == [(250, 250)]
    assert len(state("kns_person")["partitions"]) == sync_mod.PARTITIONS
    assert state("kns_person")["mode"] == "done"
    doc, updated = q("select doc, last_updated from raw.kns_person where id = 7")[0]
    assert doc["FirstName"] == "שם7"
    assert updated.isoformat() == "2026-01-01T08:00:00+00:00"
    # Naive datetimes (KNS_DocumentQuery) are read as Israel time.
    il = "at time zone 'Asia/Jerusalem'"
    assert q(f"select (last_updated {il})::text from raw.kns_documentquerie where id = 10") == [
        ("2026-02-01 09:00:00",)
    ]
    assert q(
        f"select (watermark {il})::text, last_id from ops.sync_state where table_name='kns_person'"
    ) == [("2026-01-01 10:00:00", 250)]
    assert q("select step, status, rows_out from ops.runs") == [("sync", "ok", 261)]
    logged = [
        json.loads(x)
        for f in (settings().log_dir / "sync").glob("*.jsonl")
        for x in f.read_text().splitlines()
    ]
    requests = {x["table"]: x["requests"] for x in logged if x["event"] == "table_synced"}
    # Each table's own requests: count + max_id + per partition one data page and one empty page.
    assert requests["KNS_ItemType"] == 2 + 2
    assert requests["KNS_Person"] == 2 + 2 * sync_mod.PARTITIONS


def test_crash_and_resume_without_duplicates(db, monkeypatch):
    monkeypatch.setattr(sync_mod, "FLUSH_ROWS", 100)  # checkpoint every page
    fake = FakeOData(sample_tables(1000))
    fake.fail_after["KNS_Person"] = 6  # count + max_id + 4 pages, then a crash

    with pytest.raises(RuntimeError, match="1 of 4 tables failed"):
        run_sync(
            fake, tables=["KNS_Person", "KNS_ItemType"] + ["KNS_DocumentQuerie", "V_Lobbyists"]
        )
    assert q("select count(*) from raw.kns_person") == [(400,)]
    assert state("kns_person")["partitions"][0]["last"] == 400
    assert q("select step, item_key from ops.failures") == [("sync", "KNS_Person")]

    del fake.fail_after["KNS_Person"]
    before = fake.page_calls("KNS_Person")
    run_sync(fake, tables=["KNS_Person"])
    assert q("select count(*), count(distinct id), max(id) from raw.kns_person") == [
        (1000, 1000, 1000)
    ]
    assert fake.page_calls("KNS_Person") - before == 7  # pages 5..10 + the empty end page


def test_incremental_fetches_only_recent_changes(db):
    fake = FakeOData(sample_tables(250))
    run_sync(fake, tables=["KNS_Person", "KNS_ItemType"])

    fake.tables["KNS_Person"][4] = person(5, "2026-03-01T12:00:00+02:00") | {"FirstName": "חדש"}
    fake.tables["KNS_Person"].append(person(251, "2026-03-01T12:30:00+02:00"))
    fake.calls.clear()
    written = run_sync(fake, tables=["KNS_Person", "KNS_ItemType"])

    assert written == {"KNS_Person": 2, "KNS_ItemType": 0}  # ItemType re-walked, nothing changed
    assert q("select doc->>'FirstName' from raw.kns_person where id = 5") == [("חדש",)]
    assert q("select count(*) from raw.kns_person") == [(251,)]
    person_filters = [u.params["$filter"] for u in fake.calls if u.path.endswith("KNS_Person")]
    # watermark 2026-01-01 10:00 Israel = 08:00Z, minus 48 h
    assert person_filters[0] == "LastUpdatedDate gt 2025-12-30T08:00:00Z and Id gt 0"
    assert "watermark" not in state("kns_person")


def test_deletes_move_rows_to_deleted_table(db):
    fake = FakeOData(sample_tables(250))
    run_sync(fake, tables=["KNS_ItemType"])
    fake.tables["KNS_ItemType"] = [r for r in fake.tables["KNS_ItemType"] if r["Id"] != 4]

    assert run_sync(fake, tables=["KNS_ItemType"], deletes=True) == {"KNS_ItemType": 1}
    assert q("select id from raw.kns_itemtype order by id") == [(1,), (2,), (5,), (12,)]
    assert q("select table_name, id, doc->>'Desc' from raw._deleted") == [
        ("kns_itemtype", 4, "type 4")
    ]


def test_deletes_refuse_mass_deletion(db):
    fake = FakeOData(sample_tables(250))
    run_sync(fake, tables=["KNS_Person"])
    fake.tables["KNS_Person"] = fake.tables["KNS_Person"][:200]  # 50 of 250 vanish (> 10%)
    with pytest.raises(RuntimeError, match="tables failed"):
        run_sync(fake, tables=["KNS_Person"], deletes=True)
    assert q("select count(*) from raw.kns_person") == [(250,)]


def test_db_fixture_never_touches_real_db(db):
    with connect() as conn:
        assert conn.info.dbname == "knesset_test"
    assert isinstance(conn, psycopg.Connection)
