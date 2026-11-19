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


def parse_knessets(value: str) -> list[int] | None:
    """'25,26' → [25, 26]; 'all' → None; '' → SCOPE_KNESSETS from .env."""
    if value == "all":
        return None
    return [int(k) for k in value.split(",") if k.strip()] or settings().knessets


@app.command()
def registry(
    knessets: str = typer.Option(
        "", help="Priority Knessets, e.g. 25,26 (default: SCOPE_KNESSETS)"
    ),
) -> None:
    """Rebuild text.documents from the synced KNS_Document* tables."""
    from knesset.documents import build_registry

    counts = asyncio.run(build_registry(parse_knessets(knessets) or settings().knessets))
    for status, n in sorted(counts.items(), key=lambda x: -x[1]):
        typer.echo(f"{status:<14}{n:>9,}")


@app.command()
def extract(
    knessets: str = typer.Option("", help="e.g. 25,26 or all (default: SCOPE_KNESSETS)"),
    limit: int = typer.Option(0, help="Stop after this many documents (0: no limit)"),
    priority: int = typer.Option(
        4,
        help="Only priorities up to this (1 committee protocols, "
        "2 plenum protocols, 3 bills, 4 rest)",
    ),
) -> None:
    """Download and extract pending Word documents (needs an Israeli IP)."""
    from knesset.documents import extract as run_extract

    try:
        totals = asyncio.run(run_extract(parse_knessets(knessets), limit or None, priority))
    except odata.GeoBlockedError as e:
        typer.echo(f"ERR  {e}. Run extract from an Israeli connection.", err=True)
        raise typer.Exit(2) from e
    typer.echo(", ".join(f"{k} {v:,}" for k, v in totals.most_common()) or "nothing pending")


@app.command()
def chunk(
    knessets: str = typer.Option("", help="e.g. 25,26 or all (default: SCOPE_KNESSETS)"),
    limit: int = typer.Option(0, help="Stop after this many documents (0: no limit)"),
) -> None:
    """Chunk extracted documents (new or changed text) and resolve speakers."""
    from knesset.chunks import chunk_documents

    totals = chunk_documents(parse_knessets(knessets), limit or None)
    typer.echo(", ".join(f"{k} {v:,}" for k, v in totals.items()))


@app.command()
def speakers(
    knessets: str = typer.Option("", help="e.g. 25,26 or all (default: SCOPE_KNESSETS)"),
) -> None:
    """Re-resolve chunk speakers to dim_person in place (chunks and embeddings untouched)."""
    from knesset.chunks import resolve_speakers

    totals = resolve_speakers(parse_knessets(knessets))
    typer.echo(", ".join(f"{k} {v:,}" for k, v in totals.items()))


@app.command()
def links(
    knessets: str = typer.Option("", help="e.g. 25,26 or all (default: SCOPE_KNESSETS)"),
) -> None:
    """Enrichment tier 1: chunks → session items, speakers → person and faction."""
    from knesset.enrich.links import link

    totals = link(parse_knessets(knessets))
    typer.echo(", ".join(f"{k} {v:,}" for k, v in totals.items()))


@app.command()
def topics() -> None:
    """Load the topic taxonomy (src/knesset/enrich/topics.yaml) into text.topics."""
    from knesset.enrich.topics import load_topics

    typer.echo(f"{load_topics()} topics loaded")


@app.command()
def label(
    knessets: str = typer.Option("", help="e.g. 25,26 or all (default: SCOPE_KNESSETS)"),
    n: int = typer.Option(150, help="Size of the hand-labeled gold set"),
) -> None:
    """Hand-label chunks on a local page (http://localhost:8765) into eval/labeled_chunks.jsonl."""
    from knesset.enrich.label import serve

    serve(parse_knessets(knessets), n)


@app.command("topics-teach")
def topics_teach(
    knessets: str = typer.Option("", help="e.g. 25,26 or all (default: SCOPE_KNESSETS)"),
    n: int = typer.Option(500, help="Chunks to label with the LLM (benchmark 500, then ~25000)"),
    max_usd: float = typer.Option(0, help="Budget cap (default: TOPIC_BUDGET_USD)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Project the cost, submit nothing"),
) -> None:
    """Teacher labels: the LLM labels a stratified sample with topics (Batch API, budget cap)."""
    from knesset.enrich.llm import BudgetExceeded
    from knesset.enrich.topics_llm import teach

    try:
        result = teach(parse_knessets(knessets), n, max_usd or None, dry_run)
    except BudgetExceeded as e:
        typer.echo(f"ERR  {e}", err=True)
        raise typer.Exit(2) from e
    typer.echo(", ".join(f"{k} {v}" for k, v in result.items()))


@app.command("topics-collect")
def topics_collect() -> None:
    """Collect topic-labeling batches whose wait was interrupted."""
    from knesset.enrich.topics_llm import collect_pending

    for result in collect_pending():
        typer.echo(", ".join(f"{k} {v}" for k, v in result.items()))


@app.command("topics-eval")
def topics_eval() -> None:
    """Compare the topic classifiers on held-out LLM labels (and your hand labels)."""
    from knesset.enrich.topic_clf import evaluate

    for method, result in evaluate().items():
        typer.echo(f"{method}: {result}")


@app.command("topics-apply")
def topics_apply(
    knessets: str = typer.Option("", help="e.g. 25,26 or all (default: SCOPE_KNESSETS)"),
    method: str = typer.Option("logreg", help="logreg or knn"),
) -> None:
    """Label every chunk's topics with the classifier (free)."""
    from knesset.enrich.topic_clf import apply

    result = apply(parse_knessets(knessets), method)
    typer.echo(", ".join(f"{k} {v}" for k, v in result.items()))


@app.command()
def embed(
    knessets: str = typer.Option("", help="e.g. 25,26 or all (default: SCOPE_KNESSETS)"),
    limit: int = typer.Option(0, help="Embed at most this many chunks (0: all), e.g. 1000"),
    batch: int = typer.Option(32, help="Chunks per batch"),
    index: bool = typer.Option(False, "--index", help="Build the HNSW index afterwards"),
) -> None:
    """Embed chunks with bge-m3 on the local GPU: `uv run --extra embed knesset embed`."""
    from knesset.embed import build_index, embed_chunks

    totals = embed_chunks(parse_knessets(knessets), limit or None, batch)
    typer.echo(", ".join(f"{k} {v:,}" for k, v in totals.items()))
    if index:
        typer.echo(f"HNSW index built in {build_index():.0f}s")


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
