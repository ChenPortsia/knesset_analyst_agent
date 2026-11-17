"""An in-memory stand-in for the Knesset OData API, served through httpx.MockTransport."""

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

METADATA = (Path(__file__).parent / "fixtures" / "metadata.xml").read_text()
ISRAEL = ZoneInfo("Asia/Jerusalem")
BARE_ARRAY = {"KNS_DocumentQuery"}  # bare JSON array, camelCase keys, ignores $count


def when(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=ISRAEL)


class FakeOData:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.calls: list[httpx.URL] = []
        self.fail: dict[str, list[int]] = {}  # set name -> status codes to return first
        self.fail_after: dict[str, int] = {}  # set name -> answer 400 after this many page calls

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url)
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "$metadata":
            return httpx.Response(200, text=METADATA, headers={"content-type": "application/xml"})
        if self.fail.get(name):
            return httpx.Response(self.fail[name].pop(0))
        if name in self.fail_after:
            self.fail_after[name] -= 1
            if self.fail_after[name] < 0:
                return httpx.Response(400, text="simulated crash")
        rows = self.tables[name]
        p = request.url.params
        if p.get("$count") == "true" and name not in BARE_ARRAY:
            return httpx.Response(200, json={"@odata.count": len(rows), "value": []})
        key = "id" if name in BARE_ARRAY else "Id"
        for field, op, value in re.findall(r"(\w+) (gt|le) (\S+)", p.get("$filter", "")):
            if field == "Id":
                v = int(value)
                rows = [r for r in rows if (r[key] > v if op == "gt" else r[key] <= v)]
            else:
                rows = [r for r in rows if when(r[field]) > when(value)]
        rows = sorted(rows, key=lambda r: r[key], reverse=p.get("$orderby") == "Id desc")
        rows = rows[: min(int(p.get("$top", 100)), 100)]
        if "$select" in p:
            rows = [{k: r[k] for k in p["$select"].split(",")} for r in rows]
        payload = rows if name in BARE_ARRAY else {"value": rows}
        return httpx.Response(
            200, content=json.dumps(payload), headers={"content-type": "application/json"}
        )

    def page_calls(self, name: str) -> int:
        return sum(1 for u in self.calls if u.path.endswith(name) and "$filter" in u.params)


def person(i: int, updated: str = "2026-01-01T10:00:00+02:00") -> dict:
    return {"Id": i, "FirstName": f"שם{i}", "LastName": "כהן", "LastUpdatedDate": updated}


def sample_tables(n_people: int = 250) -> dict[str, list[dict]]:
    return {
        "KNS_Person": [person(i) for i in range(1, n_people + 1)],
        "KNS_ItemType": [{"Id": i, "Desc": f"type {i}"} for i in (1, 2, 4, 5, 12)],
        "KNS_DocumentQuery": [
            {
                "id": i,
                "filePath": f"https://fs.knesset.gov.il/q{i}.doc",
                "lastUpdatedDate": "2026-02-01T09:00:00",
            }
            for i in range(10, 13)
        ],
        "V_Lobbyists": [{"Id": i, "FullName": f"lobbyist {i}"} for i in range(1, 4)],
    }
