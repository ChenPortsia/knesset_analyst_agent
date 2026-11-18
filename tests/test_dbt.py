"""Build the dbt project on a few synthetic raw rows and check the marts."""

import json
from pathlib import Path

from typer.testing import CliRunner

from knesset import tracing
from knesset.cli import app
from knesset.db import connect

LIVE = Path(__file__).parent / "fixtures" / "live"
RLM, NIQQUD = "‏", "ִ"  # right-to-left mark; hiriq

ROWS: dict[str, list[dict]] = {
    "kns_person": [
        {"Id": 1, "FirstName": f"{RLM}י{NIQQUD}שראל ", "LastName": "כהן", "GenderDesc": "זכר"},
        {"Id": 2, "FirstName": "שרה", "LastName": "לוי", "GenderDesc": "נקבה"},
    ],
    "kns_position": [{"Id": 54, "Description": "חבר הכנסת"}],
    "kns_persontoposition": [
        {
            "Id": 10,
            "PersonID": 1,
            "PositionID": 54,
            "KnessetNum": 25,
            "FactionID": 100,
            "StartDate": "2021-04-06T00:00:00+03:00",
            "FinishDate": "2022-06-30T00:00:00+03:00",
        },
        {
            "Id": 11,
            "PersonID": 1,
            "PositionID": 54,
            "KnessetNum": 25,
            "FactionID": 101,
            "StartDate": "2022-07-01T00:00:00+03:00",
        },
        {
            "Id": 12,
            "PersonID": 2,
            "PositionID": 54,
            "KnessetNum": 25,
            "FactionID": 100,
            "StartDate": "2021-04-06T00:00:00+03:00",
        },
        {
            "Id": 13,
            "PersonID": 1,
            "PositionID": 39,
            "KnessetNum": 25,
            "GovMinistryID": 5,
            "StartDate": "2021-06-13T00:00:00+03:00",
        },
    ],
    "kns_faction": [
        {"Id": 100, "Name": "סיעה א", "KnessetNum": 25},
        {"Id": 101, "Name": "סיעה ב", "KnessetNum": 25},
    ],
    "kns_committee": [{"Id": 7, "Name": "ועדת הכספים", "KnessetNum": 25}],
    "kns_committeesession": [{"Id": 500, "CommitteeID": 7, "KnessetNum": 25}],
    "kns_cmtsessionitem": [{"Id": 1, "CommitteeSessionID": 500, "ItemID": 9000, "ItemTypeID": 2}],
    "kns_plenumsession": [{"Id": 600, "KnessetNum": 25}],
    "kns_plmsessionitem": [{"Id": 1, "PlenumSessionID": 600, "ItemID": 9000, "ItemTypeID": 2}],
    "kns_bill": [{"Id": 9000, "KnessetNum": 25, "Name": "חוק ה״בדיקה״", "StatusID": 118}],
    "kns_billinitiator": [
        {"Id": 1, "BillID": 9000, "PersonID": 1, "IsInitiator": True, "Ordinal": 1},
        {"Id": 2, "BillID": 9000, "PersonID": 2, "IsInitiator": False, "Ordinal": 2},
    ],
    "kns_billname": [],
    "kns_status": [
        {"Id": 118, "Desc": "התקבלה בקריאה שלישית", "IsActive": True},
        {"Id": 104, "Desc": "הונחה על שולחן הכנסת", "IsActive": True},
    ],
    "kns_itemtype": json.loads((LIVE / "KNS_ItemType.page.json").read_text())["value"],
    "kns_plenumvote": [
        {"Id": 700, "SessionID": 600, "ItemID": 9000, "VoteDateTime": "2022-05-01T23:30:00+03:00"},
        {"Id": 701, "SessionID": 600, "ItemID": 9000, "VoteDateTime": "2022-08-01T12:00:00+03:00"},
        {"Id": 702, "SessionID": 600, "ItemID": 9000, "VoteDateTime": "2022-06-30T15:00:00+03:00"},
    ],
    "kns_plenumvoteresult": [
        {
            "Id": 1,
            "VoteID": 700,
            "MkId": 1,
            "ResultDesc": "בעד",
            "FirstName": "ישראל",
            "LastName": "כהן-לוי",
            "VoteDate": "2022-05-01T23:30:00+03:00",
        },
        {
            "Id": 2,
            "VoteID": 700,
            "MkId": 2,
            "ResultDesc": "נגד",
            "FirstName": "שרה",
            "LastName": "לוי",
            "VoteDate": "2022-05-01T23:30:00+03:00",
        },
        {
            "Id": 3,
            "VoteID": 701,
            "MkId": 1,
            "ResultDesc": "בעד",
            "FirstName": "ישראל",
            "LastName": "כהן",
            "VoteDate": "2022-08-01T12:00:00+03:00",
        },
        {
            "Id": 4,
            "VoteID": 702,
            "MkId": 1,
            "ResultDesc": "נמנע",
            "FirstName": "ישראל",
            "LastName": "כהן",
            "VoteDate": "2022-06-30T15:00:00+03:00",
        },
    ],
    "kns_query": [{"Id": 1, "PersonID": 2, "GovMinistryID": 5, "StatusID": 104}],
    "kns_agenda": [{"Id": 1, "InitiatorPersonID": 1, "StatusID": 104}],
    "kns_knessetdates": [
        {
            "Id": 1,
            "KnessetNum": 25,
            "Name": "הכנסת ה-25",
            "Assembly": 1,
            "Plenum": 1,
            "PlenumStart": "2022-11-15T00:00:00+02:00",
            "PlenumFinish": "2023-04-02T00:00:00+03:00",
        },
        {
            "Id": 2,
            "KnessetNum": 25,
            "Name": "הכנסת ה-25",
            "Assembly": 1,
            "Plenum": 2,
            "PlenumStart": "2023-04-30T00:00:00+03:00",
            "IsCurrent": True,
        },
    ],
    "kns_govministry": [{"Id": 5, "Name": "משרד האוצר", "IsActive": True}],
}


def load_raw() -> None:
    with connect() as conn:
        for table, rows in ROWS.items():
            conn.execute(
                f'create table raw."{table}" (id bigint primary key, doc jsonb not null,'
                " last_updated timestamptz, loaded_at timestamptz not null default now())"
            )
            for row in rows:
                conn.execute(
                    f'insert into raw."{table}" (id, doc) values (%s, %s)',
                    (row["Id"], json.dumps(row, ensure_ascii=False)),
                )


def q(sql: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(sql).fetchall()


def test_dbt_build(db, monkeypatch):
    monkeypatch.setattr(tracing, "setup", lambda: None)
    load_raw()
    result = CliRunner().invoke(app, ["dbt", "build"])
    assert result.exit_code == 0, result.output

    # Faction at the time of each vote, including a vote on the last day of a membership.
    assert q("select vote_result_id, faction_id from marts.fct_vote_result order by 1") == [
        (1, 100),
        (2, 100),
        (3, 101),
        (4, 100),
    ]
    assert q("select vote_id, n_for, n_against, n_abstain from marts.fct_vote order by 1") == [
        (700, 1, 1, 0),
        (701, 1, 0, 0),
        (702, 0, 0, 1),
    ]
    # Source offsets are dropped: timestamps are Israel wall-clock time.
    assert q("select vote_datetime::text from marts.fct_vote where vote_id = 700") == [
        ("2022-05-01 23:30:00",)
    ]
    # Hebrew normalization (RLM, niqqud, trailing space) and name variants from vote results.
    first, variants, knessets = q(
        "select first_name, name_variants, mk_knessets from marts.dim_person where person_id = 1"
    )[0]
    assert first == "ישראל"
    assert {"ישראל כהן", "כהן ישראל", "ישראל כהן-לוי"} <= set(variants)
    assert knessets == [25]
    assert q("select bill_name, is_passed, initiator_ids, joiner_ids from marts.fct_bill") == [
        ('חוק ה"בדיקה"', True, [1], [2])
    ]
    assert q("select session_item_key from marts.fct_session_item order by 1") == [
        ("c:1",),
        ("p:1",),
    ]
    assert q("select knesset_num, n_sessions, is_current, finish_date from marts.dim_knesset") == [
        (25, 2, True, None)
    ]
    assert q("select ministry_name from marts.fct_query") == [("משרד האוצר",)]
