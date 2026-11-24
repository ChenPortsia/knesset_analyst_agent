"""Settings from .env, plus compute-device detection."""

from functools import cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # The repo's .env wherever the process starts (notebooks run in notebooks/); a .env in the
    # working directory overrides it. In containers the variables come from compose.
    model_config = SettingsConfigDict(env_file=(ROOT / ".env", ".env"), extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "knesset"
    postgres_password: str = "change-me-postgres"
    etl_password: str = "change-me-etl"
    agent_ro_password: str = "change-me-agent"

    phoenix_url: str = "http://localhost:6006"

    odata_concurrency: int = 8

    anthropic_api_key: str = ""  # .env only; never logged
    enrich_model: str = "claude-haiku-5-5"
    topic_budget_usd: float = 10.0  # hard cap for all topic labeling (teacher + clusters)

    # Agents (Phase 7, PLAN.md §7): models per agent, and per-question limits
    agent_orchestrator_model: str = "claude-opus-5-5"
    agent_summarizer_model: str = "claude-opus-5-5"
    agent_analyst_model: str = "claude-sonnet-5-5"
    agent_researcher_model: str = "claude-sonnet-5-5"
    agent_verifier_model: str = "claude-sonnet-5-5"
    agent_max_cost_usd: float = 0.5  # per question, all agents together
    agent_max_steps: int = 12  # agent turns per question: an orchestrator step or a specialist task
    agent_timeout_s: float = 90.0

    soffice: str = "soffice"  # LibreOffice binary (in the app image; on macOS the .app path)
    soffice_workers: int = 4

    log_dir: Path = Path("logs")
    log_retention_days: int = 90

    scope_knessets: str = "25,26"
    device: str = "auto"

    def dsn(self, user: str = "etl") -> str:
        password = self.etl_password if user == "etl" else self.agent_ro_password
        return f"postgresql://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def knessets(self) -> list[int]:
        return [int(k) for k in self.scope_knessets.split(",") if k.strip()]


@cache
def settings() -> Settings:
    return Settings()


def detect_device(preferred: str = "auto") -> str:
    """Return 'mps', 'cuda' or 'cpu'. Falls back to cpu when torch is not installed."""
    if preferred != "auto":
        return preferred
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"
