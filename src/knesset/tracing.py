"""OpenTelemetry → Phoenix, and Langfuse when its keys are in .env. Call setup() once in an
entry point; until then spans are no-ops."""

import base64
import time
from functools import cache
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from phoenix.otel import register

from knesset.config import settings

PROJECT = "knesset"


@cache
def setup(project: str = PROJECT) -> TracerProvider:
    provider = register(
        project_name=project,
        endpoint=f"{settings().phoenix_url}/v1/traces",
        protocol="http/protobuf",
        batch=True,
        verbose=False,
    )
    add_langfuse(provider)
    return provider


def add_langfuse(provider: Any) -> None:
    """Also send every span to Langfuse, keeping Phoenix's exporter: phoenix.otel's provider
    drops its default processor when another is added unless told not to."""
    if exporter := langfuse_exporter():
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(exporter), replace_default_processor=False)


def langfuse_exporter() -> object | None:
    """The same spans to Langfuse (OTLP over HTTP, Basic auth with the key pair), or None
    without keys. Keys stay in .env and are never logged."""
    s = settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    token = base64.b64encode(f"{s.langfuse_public_key}:{s.langfuse_secret_key}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=f"{s.langfuse_base_url.rstrip('/')}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {token}"},
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


def flatten(d: dict, prefix: str = "") -> dict:
    """Span attributes as flat dotted keys, whether the API nests them or not."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out |= flatten(v, f"{prefix}{k}.")
        else:
            out[f"{prefix}{k}"] = v
    return out


def recent_questions(n: int = 10, pages: int = 20) -> list[dict]:
    """The last n `question` spans in Phoenix with their attributes, newest first. Pages through
    the project's spans (at most 1,000 per request, the API's cap)."""
    url = f"{settings().phoenix_url}/v1/projects/{PROJECT}/spans"
    spans: list[dict] = []
    cursor = None
    for _ in range(pages):
        params = {"limit": 1000} | ({"cursor": cursor} if cursor else {})
        resp = httpx.get(url, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        spans += [s for s in body["data"] if s.get("name") == "question"]
        cursor = body.get("next_cursor")
        if not cursor or len(spans) >= n:
            break
    spans.sort(key=lambda s: s.get("start_time", ""), reverse=True)
    out = []
    for s in spans[:n]:
        a = flatten(s.get("attributes") or {})
        out.append(a | {"start_time": s.get("start_time"), "end_time": s.get("end_time"),
                        "trace_id": (s.get("context") or {}).get("trace_id")})  # fmt: skip
    return out
