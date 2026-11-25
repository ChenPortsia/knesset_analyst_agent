"""Paid LLM calls through the Message Batches API, under a hard budget.

Every batch is projected first (free token counting on a sample of its requests) and refused
when the projection exceeds what is left of its budget. Actual cost comes from the usage the
API reports. Batches and their request → item mapping are kept in ops.llm_batches, so an
interrupted wait can be resumed with `collect`.
"""

import json
import time
from collections.abc import Iterator
from statistics import mean
from typing import Any

import anthropic
import structlog

from knesset.config import settings
from knesset.db import connect

# $ per million tokens (input, output), standard API rates; the Batch API halves them.
PRICES = {  # $ per million tokens: input, output, cache read
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "gpt-6-luna": (0.10, 0.50, 0.01),  # OpenAI list price, Nov 2026 (agents only)
}
BATCH_DISCOUNT = 0.5
CACHE_WRITE = 1.25  # × input price (5-minute cache)
SAMPLE = 20  # requests token-counted for a projection

log = structlog.get_logger()


class BudgetExceeded(RuntimeError):
    pass


def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings().anthropic_api_key or None)


def usage_cost(model: str, usage: Any, batch: bool = True) -> float:
    """Cost of one response's usage (Batch API prices unless batch=False)."""
    pin, pout, pcache = PRICES[model]
    tokens_in = (
        usage.input_tokens * pin
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * pcache
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * pin * CACHE_WRITE
    )
    return (BATCH_DISCOUNT if batch else 1.0) * (tokens_in + usage.output_tokens * pout) / 1e6


def spent(steps: list[str]) -> float:
    """Spend so far in these steps: actual cost, or the projection for uncollected batches."""
    with connect() as conn:
        return float(
            conn.execute(
                "select coalesce(sum(coalesce(cost_usd, projected_usd)), 0) from ops.llm_batches"
                " where step = any(%s)",
                (steps,),
            ).fetchone()[0]
        )


def project(api: Any, model: str, requests: list[dict], output_tokens: int) -> float:
    """Projected batch cost: input counted on a sample, output estimated per request."""
    sample = requests[:: max(1, len(requests) // SAMPLE)][:SAMPLE]
    counted = [
        api.messages.count_tokens(
            model=model, system=r["system"], messages=r["messages"]
        ).input_tokens
        for r in sample
    ]
    pin, pout, _ = PRICES[model]
    return BATCH_DISCOUNT * len(requests) * (mean(counted) * pin + output_tokens * pout) / 1e6


def submit(
    api: Any,
    step: str,
    budget_steps: list[str],
    budget_usd: float,
    requests: dict[str, dict],
    request_map: dict[str, Any],
    output_tokens: int,
    run_id: str,
    dry_run: bool = False,
) -> tuple[str | None, float]:
    """Project, check the budget, and submit one batch. Returns (batch_id, projected_usd)."""
    model = next(iter(requests.values()))["model"]
    projected = project(api, model, list(requests.values()), output_tokens)
    remaining = budget_usd - spent(budget_steps)
    log.info(
        "llm_projection",
        step=step,
        requests=len(requests),
        projected_usd=round(projected, 4),
        remaining_usd=round(remaining, 4),
    )
    if projected > remaining:
        raise BudgetExceeded(
            f"{step}: projected ${projected:.2f} exceeds the remaining budget ${remaining:.2f}"
        )
    if dry_run:
        return None, projected
    batch = api.messages.batches.create(
        requests=[{"custom_id": cid, "params": params} for cid, params in requests.items()]
    )
    with connect() as conn:
        conn.execute(
            """insert into ops.llm_batches (batch_id, step, run_id, model, n_requests,
                   projected_usd, request_map) values (%s, %s, %s, %s, %s, %s, %s)""",
            (batch.id, step, run_id, model, len(requests), projected, json.dumps(request_map)),
        )
    log.info("llm_batch_submitted", step=step, batch_id=batch.id, requests=len(requests))
    return batch.id, projected


def pending(step: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "select batch_id from ops.llm_batches where step = %s and status = 'submitted'"
            " order by created_at",
            (step,),
        ).fetchall()
    return [r[0] for r in rows]


def collect(api: Any, batch_id: str, poll_s: float = 30) -> Iterator[tuple[Any, str | None, Any]]:
    """Wait for a batch, then yield (mapped item, response text or None, error) per request
    and record its actual cost."""
    while (batch := api.messages.batches.retrieve(batch_id)).processing_status != "ended":
        log.info("llm_batch_waiting", batch_id=batch_id, status=batch.processing_status)
        time.sleep(poll_s)
    with connect() as conn:
        model, request_map = conn.execute(
            "select model, request_map from ops.llm_batches where batch_id = %s", (batch_id,)
        ).fetchone()
    cost, errors = 0.0, 0
    for entry in api.messages.batches.results(batch_id):
        item = request_map.get(entry.custom_id)
        if entry.result.type == "succeeded":
            msg = entry.result.message
            cost += usage_cost(model, msg.usage)
            text = next((b.text for b in msg.content if b.type == "text"), None)
            yield item, text, None
        else:
            errors += 1
            yield item, None, entry.result.type
    with connect() as conn:
        conn.execute(
            "update ops.llm_batches set cost_usd = %s, status = 'collected', collected_at = now()"
            " where batch_id = %s",
            (cost, batch_id),
        )
    log.info("llm_batch_collected", batch_id=batch_id, cost_usd=round(cost, 4), errors=errors)
