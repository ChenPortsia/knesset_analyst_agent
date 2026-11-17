"""`knesset` command line."""

import httpx
import typer

from knesset import log, tracing
from knesset.config import detect_device, settings
from knesset.db import connect

app = typer.Typer(no_args_is_help=True, add_completion=False)

SCHEMAS = {"raw", "ops", "text", "marts", "app"}
ROLES = {"etl", "agent_ro", "ui_app"}


@app.callback()
def main() -> None:
    log.configure()
    tracing.setup()


def check_db() -> str:
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
