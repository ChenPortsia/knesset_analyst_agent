"""The question's budget: cost, agent turns and wall time, shared by every agent (thread-safe).
Model calls are charged by `models.BudgetCallback`; graph steps and specialist tasks call
`step`. A hit limit raises LimitReached, which the graph turns into an early, honest answer."""

import threading
import time

from knesset.agents.contracts import Usage
from knesset.config import settings


class LimitReached(RuntimeError):
    """The question's cost, step or time limit was hit."""


class Budget:
    def __init__(self, max_cost_usd: float, max_steps: int, timeout_s: float):
        self.max_cost_usd, self.max_steps, self.timeout_s = max_cost_usd, max_steps, timeout_s
        self.usage = Usage()
        self.steps = 0
        self.started = time.monotonic()
        self.lock = threading.Lock()

    @classmethod
    def from_settings(cls) -> "Budget":
        s = settings()
        return cls(s.agent_max_cost_usd, s.agent_max_steps, s.agent_timeout_s)

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
