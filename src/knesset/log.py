"""Structured JSON logs to logs/<step>/<date>.jsonl, plus a run/step helper writing ops.runs."""

import json
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from knesset.config import settings
from knesset.tracing import tracer

log = structlog.get_logger()


def _to_file(_logger: Any, _name: str, event: dict) -> dict:
    path = settings().log_dir / event.get("step", "misc") / f"{date.today():%Y-%m-%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return event


def configure() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _to_file,
            structlog.processors.JSONRenderer(ensure_ascii=False, default=str),
        ],
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    )
    prune(settings().log_dir, settings().log_retention_days)


def prune(log_dir: Path, days: int) -> None:
    cutoff = time.time() - timedelta(days=days).total_seconds()
    for f in log_dir.glob("*/*.jsonl"):
        if f.stat().st_mtime < cutoff:
            f.unlink()


@dataclass
class Run:
    run_id: str
    step: str
    params: dict = field(default_factory=dict)
    rows_in: int = 0
    rows_out: int = 0
    cost_usd: float = 0.0
    trace_id: str = ""


@contextmanager
def run(step: str, record: bool = True, **params: Any) -> Iterator[Run]:
    """Time a pipeline step: bind run_id/step to every log line and write one ops.runs row."""
    r = Run(run_id=uuid.uuid4().hex[:12], step=step, params=params)
    structlog.contextvars.bind_contextvars(run_id=r.run_id, step=step)
    started = datetime.now(UTC)
    log.info("step_started", **params)
    status, error = "ok", None
    try:
        with tracer().start_as_current_span(step) as span:
            span.set_attribute("run_id", r.run_id)
            r.trace_id = format(span.get_span_context().trace_id, "032x")
            yield r
    except BaseException as e:  # includes cancellation and Ctrl-C
        status, error = "failed", repr(e)
        log.exception("step_failed")
        raise
    finally:
        finished = datetime.now(UTC)
        secs = (finished - started).total_seconds()
        throughput = r.rows_out / secs if secs else None
        log.info("step_finished", status=status, seconds=round(secs, 3), rows_out=r.rows_out)
        if record:
            _record(r, started, finished, status, error, throughput)
        structlog.contextvars.unbind_contextvars("run_id", "step")


def _record(r: Run, started, finished, status, error, throughput) -> None:
    from knesset.db import connect

    with connect() as conn:
        conn.execute(
            """insert into ops.runs (run_id, step, started_at, finished_at, status, rows_in,
                   rows_out, throughput, cost_usd, error, params)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                r.run_id,
                r.step,
                started,
                finished,
                status,
                r.rows_in,
                r.rows_out,
                throughput,
                r.cost_usd,
                error,
                json.dumps(r.params, default=str),
            ),
        )
