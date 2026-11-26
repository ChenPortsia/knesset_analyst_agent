"""Chat models for the agents (LangChain), and the callback that charges every call to the
question's budget.

Any LangChain chat model works: the graph gets its models from the runtime context, so a test
passes a fake and another provider (e.g. `ChatOpenAI`) is one factory away.
"""

from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import LLMResult

from knesset.agents.contracts import Usage
from knesset.agents.llm import Budget
from knesset.config import settings
from knesset.enrich.llm import CACHE_WRITE, PRICES

MAX_TOKENS = 16_000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}  # Haiku 5.5 has no server-side fallback

ModelFactory = Callable[[str, str], BaseChatModel]  # (agent, effort) → chat model


AGENTS = Path(__file__).with_name("agents.yaml")


@cache
def agents() -> dict[str, dict]:
    """Model and effort per agent (agents.yaml)."""
    return yaml.safe_load(AGENTS.read_text(encoding="utf-8"))


def model_name(agent: str) -> str:
    return agents()[agent]["model"]


def effort(agent: str) -> str:
    return agents()[agent].get("effort", "medium")


def chat_model(agent: str, effort: str = "medium", name: str | None = None) -> BaseChatModel:
    """The agent's model (`name`, else agents.yaml) with its effort: Claude with the server-side
    refusal fallback where the model has it, or an OpenAI model (`gpt-…`)."""
    name = name or model_name(agent)
    if name.startswith("gpt-"):
        from langchain_openai import ChatOpenAI  # the `openai` extra

        key = settings().openai_api_key or None
        # Responses API: OpenAI supports function calling with reasoning only there
        return ChatOpenAI(model=name, api_key=key, max_tokens=MAX_TOKENS, max_retries=2,
                          use_responses_api=True, reasoning={"effort": effort})  # fmt: skip
    from langchain_anthropic import ChatAnthropic

    extra: dict[str, Any] = {}
    if name in FALLBACK_MODELS:
        extra = {"betas": [FALLBACK_BETA], "model_kwargs": {"fallbacks": "default"}}
    return ChatAnthropic(
        model=name,
        api_key=settings().anthropic_api_key or None,  # from .env; never logged
        max_tokens=MAX_TOKENS,
        output_config={"effort": effort},
        max_retries=2,
        **extra,
    )


def factory(names: dict[str, str]) -> ModelFactory:
    """A model factory with some agents' models replaced (agent → model name)."""
    return lambda agent, effort="medium": chat_model(agent, effort, names.get(agent))


def price(model: str) -> tuple[float, float, float]:
    """List prices by model name; providers may add a date suffix (gpt-6-luna-2026-09-22)."""
    key = next((k for k in PRICES if model.startswith(k)), "claude-sonnet-5-5")
    return PRICES[key]


def usage_of(message: Any, fallback_model: str = "") -> Usage:
    """Tokens and list-price cost of one model response (LangChain usage metadata)."""
    meta = getattr(message, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    read, write = details.get("cache_read") or 0, details.get("cache_creation") or 0
    total_in, out = meta.get("input_tokens") or 0, meta.get("output_tokens") or 0
    model = (getattr(message, "response_metadata", None) or {}).get("model_name") or fallback_model
    pin, pout, pcache = price(model)
    cost = ((total_in - read - write) * pin + read * pcache + write * pin * CACHE_WRITE
            + out * pout) / 1e6  # fmt: skip
    return Usage(calls=1, tokens_in=total_in, tokens_out=out, cost_usd=cost)


class BudgetCallback(BaseCallbackHandler):
    """Stops a model call before it starts once a limit is hit, and charges each response."""

    raise_error = True  # LimitReached must reach the graph, not be logged and swallowed

    def __init__(self, budget: Budget, step: str = "other"):
        self.budget, self.step = budget, step

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
        self.budget.check()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for g in generations:
                self.budget.charge(usage_of(getattr(g, "message", None)), self.step)
