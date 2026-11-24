"""The A2A-ready contract (PLAN.md §7): every specialist is `run(AgentTask) -> AgentResult`.
Both are pydantic models and travel as JSON, in process now and over A2A in Phase 8."""

from typing import Any, Literal

from pydantic import BaseModel, Field

AgentName = Literal["analyst", "researcher", "summarizer", "verifier"]


class Evidence(BaseModel):
    type: Literal["chunk", "sql", "doc"]
    ref: str  # chunk id, SQL text or document id
    url: str | None = None


class Finding(BaseModel):
    claim: str = Field(description="One factual statement, in the question's language")
    evidence: list[Evidence] = Field(description="The chunks, documents or SQL that show it")
    confidence: Literal["high", "medium", "low"]


class Usage(BaseModel):
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.cost_usd += other.cost_usd


class AgentTask(BaseModel):
    task_id: str
    agent: AgentName
    instruction: str
    context: str = ""  # findings of the tasks this one depends on, the draft to verify, …
    filters: dict[str, Any] = {}  # from the UI sidebar: knessets, committee_ids, dates
    max_cost_usd: float = 0.5  # what is left of the question's budget


class AgentResult(BaseModel):
    task_id: str
    agent: AgentName
    findings: list[Finding] = []
    artifacts: list[dict[str, Any]] = []  # SQL results, chart specs
    verdict: dict[str, Any] | None = None  # the verifier's {status, issues}
    usage: Usage = Usage()
    error: str | None = None
