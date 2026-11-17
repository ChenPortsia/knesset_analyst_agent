"""Load OData entity sets into raw.<set> tables: full (resumable), incremental, and deletes.

Every table is `raw.<set_name lowercased>(id, doc jsonb, last_updated, loaded_at)`.
Progress lives in ops.sync_state.state:
  {"mode": "full" | "done", "partitions": [{"lo", "hi", "last", "done"}], "count", ...}
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import asyncpg
import structlog

from knesset import log as logs
from knesset.config import settings
from knesset.db import migrate
from knesset.odata import PAGE_SIZE, EntitySet, OData, row_id

PARTITIONS = 8
BIG_TABLE = 100_000  # tables above this get PARTITIONS Id ranges; below it, deletes are checked
FLUSH_ROWS = 10 * PAGE_SIZE
LOOKBACK = timedelta(hours=48)
MAX_DELETE_SHARE = 0.1  # refuse to delete more than this share (and > 10 rows) in one go

log = structlog.get_logger()


@asynccontextmanager
async def db_pool() -> AsyncIterator[asyncpg.Pool]:
    # Naive source datetimes are Israel local time.
    pool = await asyncpg.create_pool(
        settings().dsn(), min_size=2, max_size=12, server_settings={"timezone": "Asia/Jerusalem"}
    )
    try:
        yield pool
    finally:
        await pool.close()


async def sync(
    tables: list[str] | None = None,
    full: bool = False,
    deletes: bool = False,
    api: OData | None = None,
) -> dict[str, int]:
    """Sync the given entity sets (default: all). Returns rows written per table."""
    migrate()
    with logs.run("sync", tables=tables, full=full, deletes=deletes) as run:
        async with api or OData(settings().odata_concurrency) as api, db_pool() as pool:
            sets = await api.metadata()
            if tables:
                wanted = {t.lower() for t in tables}
                sets = [es for es in sets if es.name.lower() in wanted]
            job = check_deletes if deletes else sync_table
            results = await asyncio.gather(
                *(job(api, pool, es, full) for es in sets), return_exceptions=True
            )
        written = {}
        for es, res in zip(sets, results, strict=True):
            if isinstance(res, BaseException):
                log.error("table_failed", table=es.name, error=repr(res))
                await record_failure(run.run_id, es.name, res)
            else:
                written[es.name] = res
        run.rows_in = api.requests
        run.rows_out = sum(written.values())
        if len(written) < len(sets):
            raise RuntimeError(f"{len(sets) - len(written)} of {len(sets)} tables failed")
    return written


async def record_failure(run_id: str, table: str, error: BaseException) -> None:
    conn = await asyncpg.connect(settings().dsn())
    try:
        await conn.execute(
            "insert into ops.failures (run_id, step, item_key, error) values ($1, 'sync', $2, $3)",
            run_id,
            table,
            repr(error),
        )
    finally:
        await conn.close()


async def sync_table(api: OData, pool: asyncpg.Pool, es: EntitySet, full: bool = False) -> int:
    started, requests = time.monotonic(), api.requests
    await ensure_table(pool, es.table)
    state, watermark = await get_state(pool, es.table)
    if full or state.get("mode") != "done":
        mode, rows = "full", await full_load(api, pool, es, {} if full else state)
    elif es.has_last_updated and watermark:
        mode, rows = "incremental", await incremental(api, pool, es, watermark)
    else:
        mode, rows = "rewalk", await full_load(api, pool, es, {})
    log.info(
        "table_synced",
        table=es.name,
        mode=mode,
        rows=rows,
        requests=api.requests - requests,
        seconds=round(time.monotonic() - started, 1),
    )
    return rows


async def full_load(api: OData, pool: asyncpg.Pool, es: EntitySet, state: dict) -> int:
    """Walk Id ranges concurrently; resumes from the per-partition checkpoints in `state`."""
    if state.get("mode") != "full":
        count, top = await api.count(es), await api.max_id(es)
        n = PARTITIONS if (count or 0) > BIG_TABLE else 1
        step = -(-top // n) or 1
        parts = [{"lo": i * step, "hi": (i + 1) * step, "last": i * step} for i in range(n)]
        parts[-1]["hi"] = None  # open-ended, so rows added during the load are caught
        state = {"mode": "full", "count": count, "max_id": top, "partitions": parts}
        await save_state(pool, es.table, state)
    rows = await asyncio.gather(
        *(
            walk(api, pool, es, after=p["last"], hi=p["hi"], part=i)
            for i, p in enumerate(state["partitions"])
            if not p.get("done")
        )
    )
    await finish(pool, es.table, {**state, "mode": "done", "finished_at": now_iso()})
    return sum(rows)


async def incremental(api: OData, pool: asyncpg.Pool, es: EntitySet, watermark: datetime) -> int:
    since = (watermark - LOOKBACK).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = await walk(api, pool, es, after=0, since=since)
    state, _ = await get_state(pool, es.table)
    await finish(pool, es.table, {**state, "last_incremental": now_iso()})
    return rows


async def walk(
    api: OData,
    pool: asyncpg.Pool,
    es: EntitySet,
    after: int,
    hi: int | None = None,
    since: str = "",
    part: int | None = None,
) -> int:
    """Keyset-page Id > after (≤ hi), flushing every FLUSH_ROWS rows with a checkpoint."""
    buf: list[dict] = []
    written = 0
    while True:
        new = [r for r in await api.page(es, after, hi, since) if row_id(r) > after]
        if not new:  # also stops endpoints that ignore $filter
            break
        buf += new
        after = max(row_id(r) for r in new)
        if len(buf) >= FLUSH_ROWS:
            written += await flush(pool, es.table, buf, part, after)
            buf = []
    return written + await flush(pool, es.table, buf, part, after, done=True)


async def flush(
    pool: asyncpg.Pool, table: str, rows: list[dict], part: int | None, last: int, done=False
) -> int:
    """COPY to a temp table, upsert changed rows and move the checkpoint, in one transaction."""
    async with pool.acquire() as conn, conn.transaction():
        written = 0
        if rows:
            await conn.execute(
                "create temp table if not exists _stage (id bigint, doc text) on commit delete rows"
            )
            records = [(row_id(r), json.dumps(r, ensure_ascii=False)) for r in rows]
            await conn.copy_records_to_table("_stage", records=records)
            status = await conn.execute(f"""
                insert into raw."{table}" (id, doc, last_updated, loaded_at)
                select distinct on (id) id, doc::jsonb, nullif(coalesce(
                    doc::jsonb ->> 'LastUpdatedDate', doc::jsonb ->> 'lastUpdatedDate'), '')
                    ::timestamptz, now()
                from _stage order by id
                on conflict (id) do update set doc = excluded.doc,
                    last_updated = excluded.last_updated, loaded_at = excluded.loaded_at
                where raw."{table}".doc is distinct from excluded.doc""")
            written = int(status.split()[-1])
        if part is not None:
            await conn.execute(
                """update ops.sync_state set updated_at = now(), state = jsonb_set(state,
                       array['partitions', $2], (state->'partitions'->$3::int)
                       || jsonb_build_object('last', $4::bigint, 'done', $5::bool))
                   where table_name = $1""",
                table,
                str(part),
                part,
                last,
                done,
            )
    return written


async def check_deletes(api: OData, pool: asyncpg.Pool, es: EntitySet, _full=False) -> int:
    """Move rows that no longer exist at the source into raw._deleted (small tables only)."""
    count = await api.count(es)
    if count is None or count > BIG_TABLE:
        return 0
    live: set[int] = set()
    after = 0
    while new := [row_id(r) for r in await api.page(es, after, select="Id") if row_id(r) > after]:
        live.update(new)
        after = max(new)
    async with pool.acquire() as conn, conn.transaction():
        stored = [r["id"] for r in await conn.fetch(f'select id from raw."{es.table}"')]
        gone = [i for i in stored if i not in live]
        if len(gone) > max(10, MAX_DELETE_SHARE * len(stored)):
            raise RuntimeError(f"refusing to delete {len(gone)} of {len(stored)} rows")
        if gone:
            await conn.execute(
                f"""insert into raw._deleted (table_name, id, doc)
                    select $1, id, doc from raw."{es.table}" where id = any($2::bigint[])""",
                es.table,
                gone,
            )
            await conn.execute(f'delete from raw."{es.table}" where id = any($1::bigint[])', gone)
    log.info("deletes_checked", table=es.name, live=len(live), deleted=len(gone))
    return len(gone)


async def ensure_table(pool: asyncpg.Pool, table: str) -> None:
    await pool.execute(f"""
        create table if not exists raw."{table}" (
            id bigint primary key,
            doc jsonb not null,
            last_updated timestamptz,
            loaded_at timestamptz not null default now())""")


async def get_state(pool: asyncpg.Pool, table: str) -> tuple[dict, datetime | None]:
    row = await pool.fetchrow(
        "select state::text, watermark from ops.sync_state where table_name = $1", table
    )
    return (json.loads(row["state"]), row["watermark"]) if row else ({}, None)


async def save_state(pool: asyncpg.Pool, table: str, state: dict) -> None:
    await pool.execute(
        """insert into ops.sync_state (table_name, state) values ($1, $2::jsonb)
           on conflict (table_name) do update set state = excluded.state, updated_at = now()""",
        table,
        json.dumps(state),
    )


async def finish(pool: asyncpg.Pool, table: str, state: dict) -> None:
    """Save state and refresh the watermark (max LastUpdatedDate) and last_id from the raw table."""
    await save_state(pool, table, state)
    await pool.execute(
        f"""update ops.sync_state s set watermark = r.wm, last_id = r.last_id, updated_at = now()
            from (select max(last_updated) wm, max(id) last_id from raw."{table}") r
            where s.table_name = $1""",
        table,
    )


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
