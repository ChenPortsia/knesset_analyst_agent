"""`knesset` command line."""

import asyncio
import json
import os
from pathlib import Path

import httpx
import typer

from knesset import log, odata, tracing
from knesset.config import detect_device, settings
from knesset.db import connect, migrate

app = typer.Typer(no_args_is_help=True, add_completion=False)

SCHEMAS = {"raw", "ops", "text", "marts", "app"}
ROLES = {"etl", "agent_ro", "ui_app"}


@app.callback()
def main() -> None:
    log.configure()
    tracing.setup()


def check_db() -> str:
    migrate()
    with connect() as conn:
        version = conn.execute("show server_version").fetchone()[0]
        vector = conn.execute(
            "select extversion from pg_extension where extname = 'vector'"
        ).fetchone()
        schemas = {r[0] for r in conn.execute("select nspname from pg_namespace")}
        roles = {r[0] for r in conn.execute("select rolname from pg_roles")}
    missing = (SCHEMAS - schemas) | (ROLES - roles) | (set() if vector else {"vector"})
    if missing:
        raise RuntimeError(f"missing: {', '.join(sorted(missing))}")
    return f"postgres {version}, pgvector {vector[0]}"


def check_agent_ro() -> str:
    with connect("agent_ro") as conn:
        timeout = conn.execute("show statement_timeout").fetchone()[0]
    return f"statement_timeout={timeout}"


def check_phoenix() -> str:
    url = settings().phoenix_url
    httpx.get(f"{url}/healthz", timeout=5).raise_for_status()
    return url


@app.command()
def status() -> None:
    """Check database, roles, Phoenix and compute device; emit a test log line and span."""
    results: dict[str, tuple[bool, str]] = {}
    for name, check in [("db", check_db), ("agent_ro", check_agent_ro), ("phoenix", check_phoenix)]:
        try:
            results[name] = (True, check())
        except Exception as e:
            results[name] = (False, str(e).splitlines()[0] if str(e) else repr(e))
    results["device"] = (True, detect_device(settings().device))
    results["scope"] = (True, f"knessets {settings().knessets}")

    with log.run("status", record=results["db"][0]) as r:
        r.rows_out = sum(ok for ok, _ in results.values())
        log.log.info("status", **{k: v[1] for k, v in results.items()})
    tracing.flush()
    if results["phoenix"][0] and not tracing.trace_in_phoenix(r.trace_id):
        results["phoenix"] = (False, f"test span {r.trace_id} not received (auth on?)")
    results["trace"] = (results["phoenix"][0], tracing.trace_link(r.trace_id))
    typer.echo(f"run_id   {r.run_id}")

    for name, (ok, detail) in results.items():
        typer.echo(f"{'ok ' if ok else 'ERR'}  {name:<9}{detail}")
    if not all(ok for ok, _ in results.values()):
        raise typer.Exit(1)


@app.command("migrate")
def migrate_cmd() -> None:
    """Apply new sql/NNN_*.sql migrations."""
    typer.echo("\n".join(migrate()) or "up to date")


@app.command("sync")
def sync_cmd(
    tables: str = typer.Option("", help="Comma-separated entity sets (default: all)"),
    full: bool = typer.Option(False, "--full", help="Restart a full load, ignoring checkpoints"),
    deletes: bool = typer.Option(False, "--deletes", help="Find rows deleted at the source"),
) -> None:
    """Load OData sets into raw.*: resumes full loads, else incremental (or re-walk)."""
    from knesset.sync import sync

    names = [t.strip() for t in tables.split(",") if t.strip()] or None
    try:
        written = asyncio.run(sync(names, full=full, deletes=deletes))
    except odata.GeoBlockedError as e:
        typer.echo(f"ERR  {e}. Run sync from an Israeli connection.", err=True)
        raise typer.Exit(2) from e
    typer.echo(f"{len(written)} tables, {sum(written.values())} rows written")


FIXTURE_SETS = ["KNS_Person", "KNS_ItemType", "KNS_DocumentQuerie", "V_Lobbyists"]


@app.command("record-fixtures")
def record_fixtures(out: Path = Path("tests/fixtures/live")) -> None:
    """Save a few real API responses for offline tests (needs an Israeli IP)."""

    async def run() -> None:
        async with odata.OData() as api:
            out.mkdir(parents=True, exist_ok=True)
            resp = await api.fetch(f"{odata.BASE}/$metadata")
            (out / "metadata.xml").write_text(resp.text, encoding="utf-8")
            sets = {es.name: es for es in odata.parse_metadata(resp.text)}
            for name in FIXTURE_SETS:
                es = sets[name]
                page = await api.get(es.url, {"$top": "3", "$orderby": "Id"})
                if isinstance(page, list):
                    page = page[:3]  # bare-array endpoint may ignore $top
                count = await api.get(es.url, {"$count": "true", "$top": "0"})
                for kind, payload in [("page", page), ("count", count)]:
                    path = out / f"{name}.{kind}.json"
                    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
            typer.echo(f"{len(sets)} sets in metadata; fixtures written to {out}")

    asyncio.run(run())


DBT_DIR = Path(__file__).resolve().parents[2] / "dbt"


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def dbt(ctx: typer.Context) -> None:
    """Run dbt with the connection from .env, e.g. `knesset dbt build`."""
    from dbt.cli.main import dbtRunner

    s = settings()
    os.environ.update(
        DB_HOST=s.db_host, DB_PORT=str(s.db_port), DB_NAME=s.db_name, ETL_PASSWORD=s.etl_password
    )
    args = ctx.args or ["build"]
    with log.run("dbt", args=args):
        result = dbtRunner().invoke(
            [*args, "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]
        )
    if not result.success:
        raise typer.Exit(1)
