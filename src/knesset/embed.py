"""bge-m3 embeddings for text.chunks (1024 dims, normalized, stored as halfvec).

Runs natively where the GPU is (`uv run --extra embed knesset embed`): Docker
on macOS cannot use the Apple GPU. Chunks with the same text share one embedding.
"""

import time
from collections.abc import Callable, Sequence

import structlog

from knesset import log as logs
from knesset.config import detect_device, settings
from knesset.db import connect, migrate

MODEL = "BAAI/bge-m3"
DIM = 1024

Encoder = Callable[[list[str]], Sequence[Sequence[float]]]
log = structlog.get_logger()


def load_encoder(device: str) -> Encoder:
    from sentence_transformers import SentenceTransformer  # the `embed` extra

    model = SentenceTransformer(MODEL, device=device)
    if device in ("mps", "cuda"):
        model.half()
    model.max_seq_length = 1024  # chunks are ~500 tokens; headroom for long sentences

    def encode(texts: list[str]) -> Sequence[Sequence[float]]:
        return model.encode(texts, batch_size=len(texts), normalize_embeddings=True)

    return encode


REUSE_SQL = """
update text.chunks c set embedding = s.embedding
from (select distinct on (chunk_hash) chunk_hash, embedding
      from text.chunks where embedding is not null order by chunk_hash) s
where c.embedding is null and c.chunk_hash = s.chunk_hash
"""


def embed_chunks(
    knessets: list[int] | None,
    limit: int | None = None,
    batch: int = 32,
    encoder: Encoder | None = None,
) -> dict[str, float]:
    """Embed chunks without an embedding, shortest first, one commit per batch."""
    migrate()
    device = detect_device(settings().device)
    with logs.run("embed", knessets=knessets, limit=limit, batch=batch, device=device) as run:
        with connect() as conn:
            reused = conn.execute(REUSE_SQL).rowcount
            conn.commit()
            ids = [
                r[0]
                for r in conn.execute(
                    """select chunk_id from (
                           select distinct on (c.chunk_hash) c.chunk_id, c.n_tokens
                           from text.chunks c join text.documents d using (doc_id)
                           where c.embedding is null
                             and (%(k)s::int[] is null or d.knesset_num = any(%(k)s))
                           order by c.chunk_hash, c.chunk_id) t
                       order by n_tokens, chunk_id limit %(limit)s""",
                    {"k": knessets, "limit": limit},
                )
            ]
            encode = encoder or load_encoder(device)
            started, done = time.monotonic(), 0
            for i in range(0, len(ids), batch):
                rows = conn.execute(
                    "select chunk_id, text from text.chunks where chunk_id = any(%s)",
                    (ids[i : i + batch],),
                ).fetchall()
                vectors = encode([text for _, text in rows])
                with conn.cursor() as cur:
                    cur.execute(
                        "create temp table if not exists _emb (chunk_id bigint, v text)"
                        " on commit delete rows"
                    )
                    with cur.copy("copy _emb (chunk_id, v) from stdin") as copy:
                        for (chunk_id, _), vec in zip(rows, vectors, strict=True):
                            copy.write_row((chunk_id, "[" + ",".join(map(str, vec)) + "]"))
                    cur.execute(
                        "update text.chunks c set embedding = e.v::halfvec"
                        " from _emb e where c.chunk_id = e.chunk_id"
                    )
                conn.commit()
                done += len(rows)
                if (i // batch) % 50 == 0:
                    rate = done / max(time.monotonic() - started, 1e-9)
                    log.info("embed_progress", done=done, total=len(ids), per_s=round(rate, 1))
            copied = conn.execute(REUSE_SQL).rowcount  # duplicates of what was just embedded
            conn.commit()
        seconds = time.monotonic() - started
        per_s = round(done / seconds, 1) if seconds else 0.0
        totals = {"embedded": done, "reused": reused + copied, "per_s": per_s}
        run.rows_in, run.rows_out = len(ids), done
        log.info("embed_done", **totals)
    return totals


def build_index() -> float:
    """HNSW index for cosine search; build it after the bulk embed. Returns seconds."""
    started = time.monotonic()
    with logs.run("embed_index"), connect() as conn:
        conn.execute("set maintenance_work_mem = '2GB'")
        conn.execute(
            "create index if not exists chunks_embedding_hnsw on text.chunks"
            " using hnsw (embedding halfvec_cosine_ops)"
        )
    return time.monotonic() - started
