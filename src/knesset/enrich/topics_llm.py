"""Teacher labels: Claude assigns 1–3 taxonomy topics to a stratified sample of chunks.

The labels train a classifier on the chunk embeddings (topic_clf.py), which then labels every
chunk for free. Paid calls go through llm.py (Batch API, hard budget TOPIC_BUDGET_USD).
"""

import hashlib
import json
from typing import Any

import structlog

from knesset import log as logs
from knesset.chunks import STRUCTURAL_TAGS
from knesset.config import settings
from knesset.db import connect, migrate
from knesset.enrich import llm

STEP = "topics_llm"
BUDGET_STEPS = ["topics_llm", "topics_clusters"]  # share TOPIC_BUDGET_USD
PER_REQUEST = 10  # chunks per request
MAX_CHARS = 1200  # a chunk's topic is clear from its opening (~400 tokens)
MIN_TOKENS = 60
OUTPUT_TOKENS = 15 * PER_REQUEST + 30  # projection: `{"i": 3, "topics": [31, 33]}` per chunk

log = structlog.get_logger()

INSTRUCTIONS = """You label passages from Israeli Knesset documents (committee and plenum \
protocols, bills, motions, parliamentary questions) with topics from a fixed taxonomy.

For each numbered passage, choose 1 to 3 topic ids from the list below: the main subject \
first, then up to two more only if the passage substantially discusses them. Label what the \
passage is about, not who speaks. Use 17 (parliamentary procedure) only for procedural text \
with no policy substance. Answer with JSON only.

Topics (id: Hebrew name — English name; what it covers):
"""


def taxonomy(conn: Any) -> tuple[str, list[int], str]:
    """(taxonomy text for the prompt, child topic ids, taxonomy version hash)."""
    rows = conn.execute(
        """select t.topic_id, t.name_he, t.name_en, coalesce(t.description, ''), p.name_he
           from text.topics t join text.topics p on p.topic_id = t.parent_id
           order by t.topic_id"""
    ).fetchall()
    if not rows:
        raise RuntimeError("no topics loaded: run `knesset topics` first")
    lines = [f"{i}: {he} — {en}; {covers} [{parent}]" for i, he, en, covers, parent in rows]
    text = "\n".join(lines)
    return text, [r[0] for r in rows], hashlib.sha256(text.encode()).hexdigest()[:12]


def schema(ids: list[int]) -> dict:
    return {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer"},
                        "topics": {
                            "type": "array",
                            "items": {"type": "integer", "enum": ids},
                            "minItems": 1,
                            "maxItems": 3,
                        },
                    },
                    "required": ["i", "topics"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["labels"],
        "additionalProperties": False,
    }


SAMPLE_SQL = """
select chunk_id, text from (
    select distinct on (c.chunk_hash) c.chunk_id, c.text,
           row_number() over (partition by d.parent_type, d.committee_id,
                                           date_part('year', d.doc_date)
                              order by md5(c.chunk_hash || %(seed)s)) as rn
    from text.chunks c join text.documents d using (doc_id)
    where c.meta->>'kind' in ('turns', 'paragraphs') and c.n_tokens >= %(min_tokens)s
      and coalesce(c.speaker_role, '') <> all(%(structural)s)
      and (%(k)s::int[] is null or d.knesset_num = any(%(k)s))
      and not exists (select 1 from text.topic_labels l
                      where l.chunk_id = c.chunk_id and l.source = %(source)s)
    order by c.chunk_hash, c.chunk_id
) t
order by rn, md5(chunk_id::text || %(seed)s)  -- round-robin over strata: spread before depth
limit %(n)s
"""


def sample(conn: Any, knessets: list[int] | None, n: int, source: str, seed: str) -> list:
    return conn.execute(
        SAMPLE_SQL,
        {
            "k": knessets,
            "n": n,
            "source": source,
            "seed": seed,
            "min_tokens": MIN_TOKENS,
            "structural": list(STRUCTURAL_TAGS),
        },
    ).fetchall()


def build_requests(
    chunks: list[tuple[int, str]], model: str, system: str, ids: list[int], run_id: str
) -> tuple[dict[str, dict], dict[str, list[int]]]:
    requests, request_map = {}, {}
    for k in range(0, len(chunks), PER_REQUEST):
        group = chunks[k : k + PER_REQUEST]
        body = "\n\n".join(f"[{i}] {text[:MAX_CHARS]}" for i, (_, text) in enumerate(group, 1))
        cid = f"{run_id}-{k // PER_REQUEST}"
        requests[cid] = {
            "model": model,
            "max_tokens": 1024,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": body}],
            "thinking": {"type": "disabled"},
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": schema(ids)},
            },
        }
        request_map[cid] = [chunk_id for chunk_id, _ in group]
    return requests, request_map


def teach(
    knessets: list[int] | None,
    n: int,
    max_usd: float | None = None,
    dry_run: bool = False,
    api: Any = None,
    poll_s: float = 30,
) -> dict[str, Any]:
    """Label a stratified sample of n chunks with the LLM, then collect the batch."""
    migrate()
    api = api or llm.client()
    model = settings().enrich_model
    with logs.run(STEP, knessets=knessets, n=n, dry_run=dry_run) as run, connect() as conn:
        topics_text, ids, version = taxonomy(conn)
        chunks = sample(conn, knessets, n, "llm", seed=version)
        requests, request_map = build_requests(
            chunks, model, INSTRUCTIONS + topics_text, ids, run.run_id
        )
        batch_id, projected = llm.submit(
            api,
            STEP,
            BUDGET_STEPS,
            max_usd or settings().topic_budget_usd,
            requests,
            request_map,
            OUTPUT_TOKENS,
            run.run_id,
            dry_run,
        )
        result = {
            "chunks": len(chunks),
            "requests": len(requests),
            "projected_usd": round(projected, 4),
        }
        if batch_id:
            result |= collect(api, batch_id, model, version, poll_s)
            run.cost_usd = result["cost_usd"]
        run.rows_in, run.rows_out = len(chunks), result.get("labeled", 0)
    return result


def collect(api: Any, batch_id: str, model: str, version: str, poll_s: float = 30) -> dict:
    """Store the topic labels of one batch. Returns counts and its actual cost."""
    rows, failed = [], 0
    for chunk_ids, text, error in llm.collect(api, batch_id, poll_s):
        if error or not text:
            failed += len(chunk_ids)
            continue
        for label in json.loads(text)["labels"]:
            if 1 <= label["i"] <= len(chunk_ids) and label["topics"]:
                rows.append((chunk_ids[label["i"] - 1], label["topics"]))
    with connect() as conn:
        conn.cursor().executemany(
            """insert into text.topic_labels (chunk_id, source, topics, model, version)
               values (%s, 'llm', %s, %s, %s)
               on conflict (chunk_id, source) do update set topics = excluded.topics,
                   model = excluded.model, version = excluded.version, created_at = now()""",
            [(cid, topics, model, version) for cid, topics in rows],
        )
        cost = conn.execute(
            "select cost_usd from ops.llm_batches where batch_id = %s", (batch_id,)
        ).fetchone()[0]
    return {"labeled": len(rows), "failed": failed, "cost_usd": float(cost or 0)}


def collect_pending(api: Any = None, poll_s: float = 30) -> list[dict]:
    """Collect batches whose wait was interrupted."""
    api = api or llm.client()
    with connect() as conn:
        _, _, version = taxonomy(conn)
    return [collect(api, b, settings().enrich_model, version, poll_s) for b in llm.pending(STEP)]
