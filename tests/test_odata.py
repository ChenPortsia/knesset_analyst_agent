import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fake_odata import FakeOData, sample_tables

from knesset.odata import (
    BASE,
    LOBBYIST_BASE,
    GeoBlockedError,
    OData,
    parse_metadata,
    row_id,
    rows_of,
)

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = FIXTURES / "live"


def api_for(fake: FakeOData) -> OData:
    return OData(transport=fake.transport(), backoff_s=0)


def test_parse_metadata_applies_overrides():
    sets = {es.name: es for es in parse_metadata((FIXTURES / "metadata.xml").read_text())}
    assert sets["KNS_Person"].url == f"{BASE}/KNS_Person"
    assert sets["KNS_Person"].has_last_updated
    assert sets["KNS_Person"].table == "kns_person"
    assert not sets["KNS_ItemType"].has_last_updated
    assert sets["KNS_DocumentQuerie"].url == f"{BASE}/KNS_DocumentQuery"
    assert sets["V_Lobbyists"].url == f"{LOBBYIST_BASE}/V_Lobbyists"


def test_rows_and_ids_handle_both_shapes():
    assert rows_of({"value": [{"Id": 1}]}) == [{"Id": 1}]
    assert rows_of([{"id": 2}]) == [{"id": 2}]
    assert row_id({"Id": 1}) == 1 and row_id({"id": 2}) == 2


@pytest.mark.skipif(not LIVE.exists(), reason="run `knesset record-fixtures` from an Israeli IP")
def test_live_fixtures_parse():
    sets = {es.name: es for es in parse_metadata((LIVE / "metadata.xml").read_text())}
    assert len(sets) >= 40
    assert {"KNS_Person", "KNS_PlenumVoteResult", "V_Lobbyists"} <= sets.keys()
    assert not sets["KNS_ItemType"].has_last_updated
    for name in ["KNS_Person", "KNS_ItemType", "KNS_DocumentQuerie", "V_Lobbyists"]:
        rows = rows_of(json.loads((LIVE / f"{name}.page.json").read_text()))
        assert rows and all(row_id(r) > 0 for r in rows)


def run(coro):
    return asyncio.run(coro)


def test_pages_counts_and_max_id():
    fake = FakeOData(sample_tables(250))

    async def go():
        async with api_for(fake) as api:
            es = {s.name: s for s in await api.metadata()}["KNS_Person"]
            first = await api.page(es, after=0)
            ranged = await api.page(es, after=100, hi=150)
            return await api.count(es), await api.max_id(es), first, ranged

    count, top, first, ranged = run(go())
    assert (count, top) == (250, 250)
    assert [r["Id"] for r in first] == list(range(1, 101))
    assert [r["Id"] for r in ranged] == list(range(101, 151))


def test_bare_array_endpoint_has_no_count():
    fake = FakeOData(sample_tables())

    async def go():
        async with api_for(fake) as api:
            es = {s.name: s for s in await api.metadata()}["KNS_DocumentQuerie"]
            return await api.count(es), await api.page(es, after=0)

    count, rows = run(go())
    assert count is None
    assert [row_id(r) for r in rows] == [10, 11, 12]


def test_geo_block_redirect_raises():
    def handler(request):
        return httpx.Response(
            303, headers={"location": "https://knesset.gov.il/maintenance-page-geo"}
        )

    async def go():
        async with OData(transport=httpx.MockTransport(handler)) as api:
            await api.metadata()

    with pytest.raises(GeoBlockedError):
        run(go())


def test_retries_5xx_and_halves_concurrency_once_per_waf_episode():
    fake = FakeOData(sample_tables())
    fake.fail["KNS_Person"] = [500, 473, 481, 503]  # two WAF answers within the cooldown

    async def go():
        async with api_for(fake) as api:
            es = {s.name: s for s in await api.metadata()}["KNS_Person"]
            return await api.page(es, after=0), api.limit

    rows, limit = run(go())
    assert len(rows) == 100
    assert limit == 4


def test_gives_up_after_retries():
    fake = FakeOData(sample_tables())
    fake.fail["KNS_Person"] = [500] * 10

    async def go():
        async with OData(transport=fake.transport(), backoff_s=0, retries=3) as api:
            es = {s.name: s for s in await api.metadata()}["KNS_Person"]
            await api.page(es, after=0)

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        run(go())
