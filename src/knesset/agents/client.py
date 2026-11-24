"""How the orchestrator reaches a specialist: only through AgentClient (PLAN.md §7).
LocalClient runs it in process; A2AClient (Phase 8) will call a remote agent."""

from typing import Any, Protocol

from knesset.agents import specialist
from knesset.agents.contracts import AgentResult, AgentTask
from knesset.agents.llm import Budget


class AgentClient(Protocol):
    def run(self, task: AgentTask, budget: Budget) -> AgentResult: ...


class LocalClient:
    def __init__(self, api: Any = None):
        self.api = api  # a fake Claude client in tests

    def run(self, task: AgentTask, budget: Budget) -> AgentResult:
        return specialist.run(task, budget, self.api)


def client_for(agent: str, api: Any = None) -> AgentClient:
    """AGENT_<NAME>_MODE=a2a arrives in Phase 8; every agent is local for now."""
    return LocalClient(api)
