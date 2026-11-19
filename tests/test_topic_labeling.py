import json
import re
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace as NS
from urllib.request import Request, urlopen

import numpy as np
import pytest

from knesset.db import connect
from knesset.enrich import llm, topic_clf, topics_llm
from knesset.enrich.label import Session, handler
from knesset.enrich.topics import load_topics

TOPICS = [11, 12, 21]
TAXONOMY = """
- {id: 10, he: משפט, en: Law, topics: [{id: 11, he: חוקה, en: Constitution},
                                      {id: 12, he: בתי משפט, en: Courts}]}
- {id: 20, he: ביטחון, en: Security, topics: [{id: 21, he: צה"ל, en: Defense}]}
"""


class FakeAPI:
    """Stands in for anthropic.Anthropic: the 'model' reads the topic planted in each text."""

    def __init__(self):
        self.created: list = []
        self.messages = NS(
            count_tokens=self.count_tokens,
            batches=NS(create=self.create, retrieve=self.retrieve, results=self.results),
        )

    def count_tokens(self, **kw):
        return NS(input_tokens=2000)

    def create(self, requests):
        self.created = requests
        return NS(id=f"batch_{len(requests)}")

    def retrieve(self, batch_id):
        return NS(processing_status="ended")

    def results(self, batch_id):
        for r in self.created:
            body = r["params"]["messages"][0]["content"]
            labels = [
                {"i": int(i), "topics": [int(t)]}
                for i, t in re.findall(r"\[(\d+)\] TOPIC(\d+)", body)
            ]
            msg = NS(
                content=[NS(type="text", text=json.dumps({"labels": labels}))],
                usage=NS(
                    input_tokens=2000,
                    output_tokens=150,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )
            yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=msg))


@pytest.fixture
def data(db, tmp_path):
    taxonomy = tmp_path / "topics.yaml"
    taxonomy.write_text(TAXONOMY)
    load_topics(taxonomy)
    rng = np.random.default_rng(1)
    centers = {t: rng.normal(size=1024) for t in TOPICS}
    with connect() as conn:
        conn.execute("""insert into text.documents (doc_id, source_table, parent_type, url,
                            status, knesset_num, doc_date, committee_id)
                        values ('d:1', 'x', 'committee_session', 'u', 'done', 25,
                                '2025-01-05', 7)""")
        for k in range(300):
            t = TOPICS[k % 3]
            v = centers[t] + rng.normal(scale=0.8, size=1024)
            v /= np.linalg.norm(v)
            text = f"TOPIC{t} דיון מספר {k}"
            conn.execute(
                """insert into text.chunks (doc_id, ord, text, n_tokens, chunk_hash, meta,
                       embedding) values ('d:1', %s, %s, 100, md5(%s),
                       '{"kind": "turns"}', %s::halfvec)""",
                (k, text, text, "[" + ",".join(f"{x:.5f}" for x in v) + "]"),
            )
    return tmp_path


def q(sql: str) -> list[tuple]:
    with connect() as conn:
        return conn.execute(sql).fetchall()


def test_projection_and_budget(data):
    api = FakeAPI()
    dry = topics_llm.teach([25], 200, dry_run=True, api=api)
    assert dry["requests"] == 20 and api.created == []
    # 20 requests × (2000 in × $0.10 + 180 out × $0.50) per million, halved by the Batch API
    assert dry["projected_usd"] == pytest.approx(20 * (2000 * 0.10 + 180 * 0.50) / 1e6 / 2)
    with pytest.raises(llm.BudgetExceeded):
        topics_llm.teach([25], 200, max_usd=0.0001, api=api)
    assert api.created == []  # nothing submitted over budget


def test_teach_and_classify(data):
    result = topics_llm.teach([25], 200, api=FakeAPI(), poll_s=0)
    assert result["labeled"] == 200 and result["failed"] == 0
    cost = 20 * (2000 * 0.10 + 150 * 0.50) / 1e6 / 2  # actual usage, Batch API
    assert result["cost_usd"] == pytest.approx(cost)
    assert llm.spent(topics_llm.BUDGET_STEPS) == pytest.approx(cost)
    assert q("select count(*) from text.topic_labels where source = 'llm'") == [(200,)]
    # labels match the planted topic
    assert q("""select count(*) from text.topic_labels l join text.chunks c using (chunk_id)
                where c.text not like 'TOPIC' || l.topics[1] || '%'""") == [(0,)]

    scores = topic_clf.evaluate()
    for method in ("logreg", "knn"):
        assert scores[method]["holdout"]["micro_f1"] > 0.9, scores

    applied = topic_clf.apply([25], "knn")
    assert applied["chunks"] == 300
    wrong = q("""select count(*) from text.chunk_enrichment e join text.chunks c using (chunk_id)
                 where c.text not like 'TOPIC' || e.topics[1] || '%'""")[0][0]
    assert wrong < 15
    assert q("select distinct topics_model from text.chunk_enrichment") == [("knn@bge-m3",)]


def test_label_session(data):
    path = data / "eval" / "labels.jsonl"  # the folder does not exist yet, as in a fresh clone
    session = Session([25], 3, path)
    first = session.next()
    assert first["done"] == 0 and len(first["topics"]) == 3
    chunk_id = first["chunk"]["chunk_id"]
    with pytest.raises(ValueError):
        session.save({"chunk_id": chunk_id, "topics": []})  # at least one topic
    session.save(
        {
            "chunk_id": chunk_id,
            "topics": [12, 21, 99],
            "main": 21,
            "issue": " רישוי יבוא רחפנים ",
            "sentiment": "neutral",
        }
    )
    row = json.loads(path.read_text().splitlines()[0])
    assert row["topics"] == [21, 12] and row["main_topic"] == 21  # main first, 99 dropped
    assert row["issue"] == "רישוי יבוא רחפנים" and row["stance"] is None
    second = session.next()["chunk"]["chunk_id"]
    session.skip(second)
    assert session.next()["chunk"]["chunk_id"] not in (chunk_id, second)
    assert q("select topics from text.topic_labels where source = 'user'") == [([21, 12],)]
    # a new session continues: the saved chunk is not offered again
    assert Session([25], 3, path).next()["done"] == 1


def test_label_server(data):
    session = Session([25], 2, data / "labels.jsonl")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(session))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert "תיוג קטעים" in urlopen(url).read().decode()
        state = json.loads(urlopen(f"{url}/api/next").read())
        body = json.dumps({"chunk_id": state["chunk"]["chunk_id"], "topics": [11]}).encode()
        after = json.loads(urlopen(Request(f"{url}/api/save", data=body, method="POST")).read())
        assert after["done"] == 1
    finally:
        server.shutdown()
