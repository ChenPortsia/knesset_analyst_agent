"""OpenTelemetry → Phoenix. Call setup() once in an entry point; until then spans are no-ops."""

import time
from functools import cache

import httpx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from phoenix.otel import register

from knesset.config import settings

PROJECT = "knesset"


@cache
def setup(project: str = PROJECT) -> TracerProvider:
    return register(
        project_name=project,
        endpoint=f"{settings().phoenix_url}/v1/traces",
        protocol="http/protobuf",
        batch=True,
        verbose=False,
    )


def instrument_llm() -> None:
    """Trace every Claude call (tokens, latency) as a child span in Phoenix."""
    from openinference.instrumentation.anthropic import AnthropicInstrumentor

    instrumentor = AnthropicInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(tracer_provider=setup())


def tracer(name: str = "knesset") -> trace.Tracer:
    return trace.get_tracer(name)


def flush() -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush()


def trace_in_phoenix(trace_id: str, wait_s: float = 10) -> bool:
    """Poll the Phoenix REST API until the trace shows up (spans are exported in batches)."""
    url = f"{settings().phoenix_url}/v1/projects/{PROJECT}/spans"
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        resp = httpx.get(url, params={"trace_id": trace_id, "limit": 1}, timeout=5)
        if resp.status_code == 200 and resp.json()["data"]:
            return True
        time.sleep(0.5)
    return False


def trace_link(trace_id: str) -> str:
    return f"{settings().phoenix_url}/redirects/traces/{trace_id}"
