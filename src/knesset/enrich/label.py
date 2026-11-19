"""Hand labels for evaluation (~150 chunks): topics, sentiment, stance.

`knesset label` shows one chunk at a time and saves each answer at once, to
eval/labeled_chunks.jsonl (the committed gold set) and to text.topic_labels (source 'user').
Stopping and restarting continues where it stopped.
"""

import json
from collections.abc import Callable
from pathlib import Path

from knesset.db import connect, migrate
from knesset.enrich.topics_llm import sample, taxonomy

LABELS = Path(__file__).resolve().parents[3] / "eval" / "labeled_chunks.jsonl"
SENTIMENT = {"-": "negative", "0": "neutral", "+": "positive"}
STANCE = {"f": "for", "a": "against", "n": "neutral", "": None}
SHOW_CHARS = 1500


def done(path: Path) -> set[int]:
    if not path.exists():
        return set()
    return {json.loads(line)["chunk_id"] for line in path.read_text().splitlines() if line}


def label(
    knessets: list[int] | None,
    n: int = 150,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    path: Path = LABELS,
) -> int:
    """Label until n chunks are in the gold set. Returns how many are labeled."""
    migrate()
    labeled = done(path)
    with connect() as conn:
        topics_text, ids, version = taxonomy(conn)
        todo = [
            c
            for c in sample(conn, knessets, n + len(labeled), "user", seed="gold")
            if c[0] not in labeled
        ][: max(0, n - len(labeled))]
        meta = {
            r[0]: r[1:]
            for r in conn.execute(
                """select c.chunk_id, c.doc_id, c.speaker_raw, d.doc_date::date::text
                   from text.chunks c join text.documents d using (doc_id)
                   where c.chunk_id = any(%s)""",
                ([c[0] for c in todo],),
            )
        }
    say(
        f"{len(labeled)} labeled, {len(todo)} to go. Topics: ids separated by commas "
        "(? = list, s = skip, q = quit)."
    )
    for chunk_id, text in todo:
        doc_id, speaker, date = meta[chunk_id]
        say(
            f"\n=== {len(labeled) + 1}/{n} · {doc_id} · {date} · {speaker or '-'}\n"
            f"{text[:SHOW_CHARS]}"
        )
        while True:
            answer = ask("topics> ").strip()
            if answer == "?":
                say(topics_text)
                continue
            if answer in ("q", "s"):
                break
            chosen = [int(x) for x in answer.replace(" ", "").split(",") if x.isdigit()]
            if chosen and all(t in ids for t in chosen) and len(chosen) <= 3:
                break
            say("1-3 topic ids from the list (? to show it)")
        if answer == "q":
            break
        if answer == "s":
            continue
        sentiment = ""
        while sentiment not in SENTIMENT:
            sentiment = ask("sentiment (- 0 +)> ").strip()
        stance = None
        while stance is None or stance not in STANCE:
            stance = ask("stance (f=for a=against n=neutral, Enter=none)> ").strip()
        target = ask("stance target> ").strip() if stance in ("f", "a") else ""
        row = {
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "topics": chosen,
            "sentiment": SENTIMENT[sentiment],
            "stance": STANCE[stance],
            "stance_target": target or None,
            "taxonomy_version": version,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with connect() as conn:
            conn.execute(
                """insert into text.topic_labels (chunk_id, source, topics, model, version)
                   values (%s, 'user', %s, 'human', %s)
                   on conflict (chunk_id, source) do update set topics = excluded.topics""",
                (chunk_id, chosen, version),
            )
        labeled.add(chunk_id)
    return len(labeled)
