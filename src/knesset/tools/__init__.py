"""Agent tools (PLAN.md §7) as plain typed functions; `lc.py` wraps them for LangChain.

Every tool returns compact JSON-ready data with an `evidence` list, truncates long text, and
logs and traces its call. Database access is read-only (`agent_ro`).
"""
