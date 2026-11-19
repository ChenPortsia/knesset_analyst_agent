"""Topic classifier on the bge-m3 chunk embeddings, taught by the LLM labels (topics_llm.py).

Two candidates, compared by `evaluate`: one logistic regression per topic, or a vote of the
K nearest labeled chunks. Decision rule: the best topic always, plus up to two more whose
score passes a threshold tuned on held-out labels. `apply` labels every chunk for free.
"""

from typing import Any

import numpy as np
import structlog
from pgvector.psycopg import register_vector
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.preprocessing import MultiLabelBinarizer

from knesset import log as logs
from knesset.db import connect, migrate
from knesset.enrich.topics_llm import taxonomy

MAX_TOPICS = 3
K = 15
THRESHOLDS = np.arange(0.1, 0.9, 0.05)
BATCH = 20_000

log = structlog.get_logger()


def vectors(rows: list) -> np.ndarray:
    return np.stack([np.asarray(v.to_numpy(), dtype=np.float32) for v in rows])


def load_labels(conn: Any, source: str) -> tuple[list[int], np.ndarray, list[list[int]]]:
    rows = conn.execute(
        """select l.chunk_id, c.embedding, l.topics from text.topic_labels l
           join text.chunks c using (chunk_id)
           where l.source = %s and c.embedding is not null order by l.chunk_id""",
        (source,),
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 1024), np.float32), []
    return [r[0] for r in rows], vectors([r[1] for r in rows]), [list(r[2]) for r in rows]


class TopicModel:
    def __init__(self, method: str = "logreg"):
        self.method = method
        self.mlb = MultiLabelBinarizer()

    def fit(self, X: np.ndarray, labels: list[list[int]]) -> "TopicModel":
        Y = self.mlb.fit_transform(labels)
        if self.method == "logreg":
            self.clf = OneVsRestClassifier(LogisticRegression(max_iter=2000, C=4.0))
            self.clf.fit(X, Y)
        else:
            self.X, self.Y = X, Y.astype(np.float32)
        return self

    def scores(self, X: np.ndarray) -> np.ndarray:
        if self.method == "logreg":
            return self.clf.predict_proba(X)
        sims = X @ self.X.T  # embeddings are normalized: cosine similarity
        top = np.argpartition(-sims, min(K, sims.shape[1] - 1), axis=1)[:, :K]
        w = np.take_along_axis(sims, top, axis=1).clip(min=0)
        return np.einsum("nk,nkt->nt", w, self.Y[top]) / np.maximum(w.sum(1, keepdims=True), 1e-9)

    def decide(self, scores: np.ndarray, threshold: float) -> list[list[int]]:
        out = []
        for row in scores:
            order = np.argsort(-row)[:MAX_TOPICS]
            keep = [order[0]] + [j for j in order[1:] if row[j] >= threshold]
            out.append([int(self.mlb.classes_[j]) for j in keep])
        return out


def f1(pred: list[list[int]], gold: list[list[int]]) -> dict[str, float]:
    """Micro and macro F1 over topics, and how often the first predicted topic is in gold."""
    tp: dict[int, int] = {}
    fp: dict[int, int] = {}
    fn: dict[int, int] = {}
    for p, g in zip(pred, gold, strict=True):
        for t in set(p) | set(g):
            if t in p and t in g:
                tp[t] = tp.get(t, 0) + 1
            elif t in p:
                fp[t] = fp.get(t, 0) + 1
            else:
                fn[t] = fn.get(t, 0) + 1
    topics = set(tp) | set(fp) | set(fn)

    def score(a: int, b: int, c: int) -> float:
        return 2 * a / (2 * a + b + c) if a + b + c else 0.0

    micro = score(sum(tp.values()), sum(fp.values()), sum(fn.values()))
    macro = float(np.mean([score(tp.get(t, 0), fp.get(t, 0), fn.get(t, 0)) for t in topics]))
    top1 = float(np.mean([p[0] in g for p, g in zip(pred, gold, strict=True) if p]))
    return {"micro_f1": round(micro, 3), "macro_f1": round(macro, 3), "top1": round(top1, 3)}


def evaluate(holdout: float = 0.15, seed: int = 0) -> dict[str, dict]:
    """Train on the LLM labels minus a held-out share; tune the threshold and score each method
    on it, and on the user's hand labels when there are any."""
    with connect() as conn:
        register_vector(conn)
        ids, X, labels = load_labels(conn, "llm")
        _, Xu, user = load_labels(conn, "user")
    if len(ids) < 50:
        raise RuntimeError(f"only {len(ids)} LLM labels: run `knesset topics-teach` first")
    rng = np.random.default_rng(seed)
    test = rng.random(len(ids)) < holdout
    train = ~test
    results = {}
    for method in ("logreg", "knn"):
        model = TopicModel(method).fit(X[train], [labels[i] for i in np.flatnonzero(train)])
        gold = [labels[i] for i in np.flatnonzero(test)]
        s = model.scores(X[test])
        best = max(THRESHOLDS, key=lambda t: f1(model.decide(s, t), gold)["micro_f1"])
        results[method] = {
            "threshold": round(float(best), 2),
            "train": int(train.sum()),
            "holdout": f1(model.decide(s, best), gold),
        }
        if user:
            results[method]["user"] = f1(model.decide(model.scores(Xu), best), user)
    log.info("topic_clf_evaluated", **{m: r["holdout"] for m, r in results.items()})
    return results


def apply(knessets: list[int] | None, method: str = "logreg") -> dict[str, Any]:
    """Fit on all LLM labels and write topics for every turn/paragraph chunk in scope."""
    migrate()
    threshold = evaluate()[method]["threshold"]
    with logs.run("topics_apply", knessets=knessets, method=method) as run, connect() as conn:
        register_vector(conn)
        _, _, version = taxonomy(conn)
        _, X, labels = load_labels(conn, "llm")
        model = TopicModel(method).fit(X, labels)
        name = f"{method}@bge-m3"
        written = 0
        with conn.cursor(name="chunk_vectors") as cur:
            cur.itersize = BATCH
            cur.execute(
                """select c.chunk_id, c.embedding from text.chunks c
                   join text.documents d using (doc_id)
                   where c.embedding is not null and c.meta->>'kind' in ('turns', 'paragraphs')
                     and (%(k)s::int[] is null or d.knesset_num = any(%(k)s))""",
                {"k": knessets},
            )
            while rows := cur.fetchmany(BATCH):
                s = model.scores(vectors([r[1] for r in rows]))
                topics = model.decide(s, threshold)
                cols = {int(t): j for j, t in enumerate(model.mlb.classes_)}
                out = [
                    (r[0], [str(t) for t in ts], [float(s[i, cols[t]]) for t in ts])
                    for i, (r, ts) in enumerate(zip(rows, topics, strict=True))
                ]
                with conn.cursor() as w:
                    w.executemany(
                        """insert into text.chunk_enrichment
                               (chunk_id, topics, topic_scores, topics_model, topics_version)
                           values (%s, %s, %s, %s, %s)
                           on conflict (chunk_id) do update set topics = excluded.topics,
                               topic_scores = excluded.topic_scores,
                               topics_model = excluded.topics_model,
                               topics_version = excluded.topics_version""",
                        [(cid, ts, sc, name, version) for cid, ts, sc in out],
                    )
                written += len(out)
                log.info("topics_apply_progress", written=written)
        run.rows_out = written
    return {"chunks": written, "method": method, "threshold": threshold}
