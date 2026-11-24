"""Claude calls for the agents: one tool loop with a structured final answer, its cost charged
to the question's budget.

- The final answer is JSON constrained by `output_config.format` (schema from a pydantic
  model); tools and the output schema go in the same request.
- Forced tool choice is not available on these models: prompts say which tools to use.
- Refusals fall back server-side (`fallbacks: "default"`).
- Tools and system prompt are a stable prefix, cached with `cache_control`.
"""

import json
import threading
import time
from collections.abc import Callable
from functools import cache
from typing import Any

import psycopg
from pydantic import BaseModel

from knesset.agents.contracts import Usage
from knesset.enrich.llm import PRICES, usage_cost
from knesset.tools.lc import tool_schema

MAX_TOKENS = 16_000
MAX_ROUNDS = 8  # model calls per tool loop; the last one may not call tools
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LimitReached(RuntimeError):
    """The question's cost, step or time limit was hit."""


class Budget:
    """Per question, shared by all agents (thread-safe): cost, agent turns and wall time."""

    def __init__(self, max_cost_usd: float, max_steps: int, timeout_s: float):
        self.max_cost_usd, self.max_steps, self.timeout_s = max_cost_usd, max_steps, timeout_s
        self.usage = Usage()
        self.steps = 0
        self.started = time.monotonic()
        self.lock = threading.Lock()

    def charge(self, usage: Usage) -> None:
        with self.lock:
            self.usage.add(usage)

    def step(self, what: str) -> None:
        with self.lock:
            self.check()
            if self.steps >= self.max_steps:
                raise LimitReached(f"step limit ({self.max_steps}) reached before {what}")
            self.steps += 1

    def check(self) -> None:
        if self.usage.cost_usd >= self.max_cost_usd:
            raise LimitReached(f"cost limit (${self.max_cost_usd:.2f}) reached")
        if time.monotonic() - self.started > self.timeout_s:
            raise LimitReached(f"time limit ({self.timeout_s:.0f} s) reached")

    @property
    def left_usd(self) -> float:
        return max(self.max_cost_usd - self.usage.cost_usd, 0.0)


@cache
def client() -> Any:
    import anthropic

    return anthropic.Anthropic()  # ANTHROPIC_API_KEY from .env (never logged)


def json_schema(model: type[BaseModel]) -> dict:
    """A pydantic model's JSON schema in the strict form structured outputs need: every object
    closed and every property required."""

    def strict(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: strict(v) for k, v in node.items() if k not in ("default", "title")}
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            return node
        if isinstance(node, list):
            return [strict(v) for v in node]
        return node

    return strict(model.model_json_schema())


def run_tool(fns: dict[str, Callable[..., dict]], name: str, args: dict) -> tuple[str, bool]:
    """A tool's JSON result, or its error as text for the model to correct (is_error)."""
    try:
        return json.dumps(fns[name](**args), ensure_ascii=False, separators=(",", ":")), False
    except (ValueError, TypeError, KeyError, psycopg.Error) as e:
        return f"Error: {e}", True


def call_cost(response: Any, model: str) -> Usage:
    served = getattr(response, "model", model)  # a fallback model bills at its own price
    price_model = served if served in PRICES else model
    u = response.usage
    return Usage(
        calls=1,
        tokens_in=u.input_tokens
        + (getattr(u, "cache_read_input_tokens", 0) or 0)
        + (getattr(u, "cache_creation_input_tokens", 0) or 0),
        tokens_out=u.output_tokens,
        cost_usd=usage_cost(price_model, u, batch=False),
    )


def tool_loop(
    model: str,
    system: str,
    prompt: str,
    output: type[BaseModel],
    budget: Budget,
    tools: list[Callable[..., dict]] = (),
    effort: str = "medium",
    api: Any = None,
) -> tuple[BaseModel, Usage, list[dict]]:
    """Run tools until the model gives its final answer as `output`. Returns the answer, this
    loop's usage, and the artifacts (each tool call with its result)."""
    api = api or client()
    fns = {fn.__name__: fn for fn in tools}
    tool_defs = [tool_schema(fn) for fn in tools]
    messages: list[dict] = [{"role": "user", "content": prompt}]
    usage = Usage()
    artifacts: list[dict] = []
    for round_ in range(1, MAX_ROUNDS + 1):
        budget.check()
        last = round_ == MAX_ROUNDS
        if last and tool_defs:
            messages.append(
                {"role": "user", "content": "Stop using tools: give your final answer now."}
            )
        kwargs: dict[str, Any] = {"tools": tool_defs} if tool_defs else {}
        if last and tool_defs:
            kwargs["tool_choice"] = {"type": "none"}
        response = api.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": json_schema(output)},
            },
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **kwargs,
        )
        cost = call_cost(response, model)
        usage.add(cost)
        budget.charge(cost)
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            text = "".join(b.text for b in response.content if b.type == "text")
            return output.model_validate_json(text), usage, artifacts
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            result, failed = run_tool(fns, call.name, dict(call.input))
            artifacts.append({"tool": call.name, "input": dict(call.input), "error": failed,
                              "output": json.loads(result) if not failed else result})  # fmt: skip
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": result,
                    "is_error": failed,
                }
            )
        messages.append({"role": "user", "content": results})
    raise RuntimeError("no final answer after the last round")
