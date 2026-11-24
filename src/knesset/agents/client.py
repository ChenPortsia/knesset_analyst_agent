"""How the orchestrator reaches a specialist: only through AgentClient (PLAN.md §7).
LocalClient runs it in process; A2AClient (Phase 8) will call a remote agent."""

from typing import Protocol

from knesset.agents import specialist
from knesset.agents.contracts import AgentResult, AgentTask
from knesset.agents.llm import Budget
from knesset.agents.models import ModelFactory, chat_model


class AgentClient(Protocol):
    def run(self, task: AgentTask, budget: Budget, models: ModelFactory) -> AgentResult: ...


class LocalClient:
    def run(
        self, task: AgentTask, budget: Budget, models: ModelFactory = chat_model
    ) -> AgentResult:
        return specialist.run(task, budget, models)


def client_for(agent: str) -> AgentClient:
    """AGENT_<NAME>_MODE=a2a arrives in Phase 8; every agent is local for now."""
    return LocalClient()
