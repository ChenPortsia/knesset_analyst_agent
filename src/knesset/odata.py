"""Knesset OData client: metadata, counts and keyset pages. See PLAN.md §4 for the API quirks."""

import asyncio
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

BASE = "https://knesset.gov.il/OdataV4/ParliamentInfo"
LOBBYIST_BASE = "https://knesset.gov.il/OdataV4/Lobbyist"
LOBBYIST_SETS = {"V_Lobbyists", "V_LobbyistsClients"}
URL_NAME_FIXES = {"KNS_DocumentQuerie": "KNS_DocumentQuery"}  # the declared name 404s
PAGE_SIZE = 100  # hard cap on the server
RETRY_STATUS = {473, 500, 502, 503, 504}

log = structlog.get_logger()


class GeoBlockedError(RuntimeError):
    """knesset.gov.il answers Israeli IPs only; others are redirected to a maintenance page."""


@dataclass(frozen=True)
class EntitySet:
    name: str
    url_name: str
    base_url: str
    columns: tuple[str, ...]
    has_last_updated: bool

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.url_name}"

    @property
    def table(self) -> str:
        return self.name.lower()


def parse_metadata(xml: str) -> list[EntitySet]:
    root = ET.fromstring(xml)
    types = {
        t.get("Name"): tuple(p.get("Name") for p in t.findall("{*}Property"))
        for t in root.findall(".//{*}EntityType")
    }
    sets = []
    for es in root.findall(".//{*}EntitySet"):
        name = es.get("Name")
        columns = types.get(es.get("EntityType", "").rsplit(".", 1)[-1], ())
        sets.append(
            EntitySet(
                name=name,
                url_name=URL_NAME_FIXES.get(name, name),
                base_url=LOBBYIST_BASE if name in LOBBYIST_SETS else BASE,
                columns=columns,
                has_last_updated="LastUpdatedDate" in columns,
            )
        )
    return sets


def rows_of(payload: Any) -> list[dict]:
    """OData wraps rows in {"value": [...]}; KNS_DocumentQuery returns a bare array."""
    return payload if isinstance(payload, list) else payload.get("value", [])


def row_id(row: dict) -> int:
    return int(row["Id"] if "Id" in row else row["id"])  # KNS_DocumentQuery uses camelCase


class OData:
    """One shared HTTP/2 client and one global concurrency limit, halved on WAF throttling (473)."""

    def __init__(
        self,
        concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = 6,
        backoff_s: float = 1.0,
    ):
        self.limit = concurrency
        self.sem = asyncio.Semaphore(concurrency)
        self.retries = retries
        self.backoff_s = backoff_s
        self.requests = 0
        self.client = httpx.AsyncClient(
            http2=transport is None,
            transport=transport,
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "knesset-data/0.1"},
        )

    async def __aenter__(self) -> "OData":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.aclose()

    async def _throttle(self) -> None:
        if self.limit > 1:
            steal = self.limit // 2
            self.limit -= steal
            log.warning("odata_throttled", concurrency=self.limit)
            for _ in range(steal):  # held for the rest of the run
                await self.sem.acquire()

    async def fetch(self, url: str, params: dict | None = None, **kw: Any) -> httpx.Response:
        """GET with retries on 5xx / 473 / timeouts; raises GeoBlockedError on the geo redirect."""
        for attempt in range(1, self.retries + 1):
            try:
                async with self.sem:
                    self.requests += 1
                    resp = await self.client.get(url, params=params, **kw)
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if "maintenance-page-geo" in location:
                        raise GeoBlockedError(
                            "knesset.gov.il redirected to its geo-block page: "
                            "the API answers Israeli IPs only"
                        )
                    raise RuntimeError(f"unexpected redirect {resp.status_code} -> {location}")
                if resp.status_code not in RETRY_STATUS:
                    return resp.raise_for_status()
                error = f"HTTP {resp.status_code}"
                if resp.status_code == 473:
                    await self._throttle()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                error = repr(e)
            if attempt == self.retries:
                raise RuntimeError(f"{url} failed after {attempt} attempts: {error}")
            delay = min(self.backoff_s * 2 ** (attempt - 1), 30) * (0.5 + random.random())
            log.warning("odata_retry", url=url, attempt=attempt, error=error, delay=round(delay, 2))
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def get(self, url: str, params: dict | None = None) -> Any:
        resp = await self.fetch(url, params)
        if "html" in resp.headers.get("content-type", ""):
            raise GeoBlockedError(f"HTML instead of JSON from {url} (geo-block?)")
        return resp.json()

    async def metadata(self) -> list[EntitySet]:
        resp = await self.fetch(f"{BASE}/$metadata", headers={"Accept": "application/xml"})
        return parse_metadata(resp.text)

    async def count(self, es: EntitySet) -> int | None:
        """Live row count, or None where the endpoint ignores $count."""
        payload = await self.get(es.url, {"$count": "true", "$top": "0"})
        return payload.get("@odata.count") if isinstance(payload, dict) else None

    async def max_id(self, es: EntitySet) -> int:
        rows = rows_of(await self.get(es.url, {"$orderby": "Id desc", "$top": "1"}))
        return max((row_id(r) for r in rows), default=0)

    async def page(
        self, es: EntitySet, after: int, hi: int | None = None, since: str = "", select: str = ""
    ) -> list:
        """Up to 100 rows with Id > after (and <= hi, and LastUpdatedDate > since), by Id."""
        filt = f"Id gt {after}"
        if hi is not None:
            filt += f" and Id le {hi}"
        if since:
            filt = f"LastUpdatedDate gt {since} and {filt}"
        params = {"$filter": filt, "$orderby": "Id", "$top": str(PAGE_SIZE)}
        if select:
            params["$select"] = select
        return rows_of(await self.get(es.url, params))
