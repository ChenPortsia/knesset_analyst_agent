"""The tools as LangChain tools, grouped by agent (PLAN.md §7). Results are compact JSON
strings; a ValueError (bad filter, bad SQL, …) is returned to the agent as text to fix."""

import json
from collections.abc import Callable

import psycopg
from langchain_core.tools import StructuredTool, ToolException

from knesset.tools.common import resolve_entity
from knesset.tools.research import facets, get_document, search_text, share_of_words
from knesset.tools.sql import describe_schema, make_chart, sql_query
from knesset.tools.summary import get_document_summary
from knesset.tools.verify import get_chunks, rerun_sql

# The orchestrator's plan / delegate / ask_user / finish are steps of the graph (agents/graph.py).
AGENT_TOOLS: dict[str, list[Callable[..., dict]]] = {
    "orchestrator": [resolve_entity],
    "analyst": [describe_schema, sql_query, resolve_entity, make_chart],
    "researcher": [search_text, get_document, facets, share_of_words, resolve_entity],
    "summarizer": [search_text, facets, share_of_words, get_document_summary, get_document],
    "verifier": [get_chunks, rerun_sql],  # its verdict is its structured answer
}


def as_tool(fn: Callable[..., dict]) -> StructuredTool:
    schema = StructuredTool.from_function(fn).args_schema

    def run(**kwargs: object) -> str:
        try:
            result = fn(**kwargs)
        except (ValueError, psycopg.Error) as e:  # the agent's mistake: tell it, so it retries
            raise ToolException(str(e)) from e
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return StructuredTool(
        name=fn.__name__,
        description=(fn.__doc__ or "").strip(),
        args_schema=schema,
        func=run,
        handle_tool_error=True,
    )


def tool_schema(fn: Callable[..., dict]) -> dict:
    """A tool definition for the Claude API (name, description, JSON schema of the arguments)."""
    schema = StructuredTool.from_function(fn).args_schema.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)  # the docstring is the tool's description already
    return {"name": fn.__name__, "description": (fn.__doc__ or "").strip(), "input_schema": schema}


def tools_for(agent: str) -> list[StructuredTool]:
    return [as_tool(fn) for fn in AGENT_TOOLS[agent]]
