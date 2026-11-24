"""Chat models for the agents (LangChain), and the callback that charges every call to the
question's budget.

Any LangChain chat model works: the graph gets its models from the runtime context, so a test
passes a fake and another provider (e.g. `ChatOpenAI`) is one factory away.
"""

from collections.abc import Callable
from typing import Any

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


def model_name(agent: str) -> str:
    return getattr(settings(), f"agent_{agent}_model")


def chat_model(agent: str, effort: str = "medium") -> BaseChatModel:
    """The agent's Claude model from .env, with its effort and, where the model has it, the
    server-side refusal fallback."""
    from langchain_anthropic import ChatAnthropic

    name = model_name(agent)
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


def usage_of(message: Any, fallback_model: str = "") -> Usage:
    """Tokens and list-price cost of one model response (LangChain usage metadata)."""
    meta = getattr(message, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    read, write = details.get("cache_read") or 0, details.get("cache_creation") or 0
    total_in, out = meta.get("input_tokens") or 0, meta.get("output_tokens") or 0
    model = (getattr(message, "response_metadata", None) or {}).get("model_name") or fallback_model
    pin, pout, pcache = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    cost = ((total_in - read - write) * pin + read * pcache + write * pin * CACHE_WRITE
            + out * pout) / 1e6  # fmt: skip
    return Usage(calls=1, tokens_in=total_in, tokens_out=out, cost_usd=cost)


class BudgetCallback(BaseCallbackHandler):
    """Stops a model call before it starts once a limit is hit, and charges each response."""

    raise_error = True  # LimitReached must reach the graph, not be logged and swallowed

    def __init__(self, budget: Budget):
        self.budget = budget

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
        self.budget.check()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for g in generations:
                self.budget.charge(usage_of(getattr(g, "message", None)))
