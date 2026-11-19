"""Hand labels for evaluation (~150 chunks): topics, specific issue, sentiment, stance.

`knesset label` serves a local page (http://localhost:8765) showing one chunk at a time.
Topics are "acceptable topics", not one right answer: check any that fit, optionally mark the
main one. Each answer is saved at once to eval/labeled_chunks.jsonl (the committed gold set)
and to text.topic_labels (source 'user'); closing the page and starting again continues.
"""

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from knesset.db import connect, migrate
from knesset.enrich.topics_llm import sample, taxonomy

LABELS = Path(__file__).resolve().parents[3] / "eval" / "labeled_chunks.jsonl"
PAGE = Path(__file__).with_name("label.html")
PORT = 8765


class Session:
    """The chunks to label and where answers go."""

    def __init__(self, knessets: list[int] | None, n: int, path: Path = LABELS):
        migrate()
        self.path, self.n = path, n
        self.done = self.read_done()
        self.skipped: set[int] = set()
        with connect() as conn:
            _, ids, self.version = taxonomy(conn)
            self.topics = [
                {"id": r[0], "he": r[1], "en": r[2], "parent": r[3]}
                for r in conn.execute(
                    """select t.topic_id, t.name_he, t.name_en, p.name_he
                       from text.topics t join text.topics p on p.topic_id = t.parent_id
                       order by t.parent_id, t.topic_id"""
                )
            ]
            self.todo = [
                c[0] for c in sample(conn, knessets, 2 * n + len(self.done), "user", seed="gold")
            ]
        self.valid = set(ids)

    def read_done(self) -> set[int]:
        if not self.path.exists():
            return set()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return {json.loads(line)["chunk_id"] for line in lines if line}

    def next(self) -> dict[str, Any]:
        state = {"done": len(self.done), "total": self.n, "topics": self.topics}
        if len(self.done) >= self.n:
            return state | {"chunk": None}
        chunk_id = next(
            (c for c in self.todo if c not in self.done and c not in self.skipped), None
        )
        if chunk_id is None:
            return state | {"chunk": None}
        with connect() as conn:
            doc_id, date, speaker, text = conn.execute(
                """select c.doc_id, d.doc_date::date::text, c.speaker_raw, c.text
                   from text.chunks c join text.documents d using (doc_id)
                   where c.chunk_id = %s""",
                (chunk_id,),
            ).fetchone()
        chunk = {
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "date": date,
            "speaker": speaker,
            "text": text,
        }
        return state | {"chunk": chunk}

    def save(self, answer: dict[str, Any]) -> None:
        chunk_id = int(answer["chunk_id"])
        topics = [int(t) for t in answer.get("topics", []) if int(t) in self.valid]
        main = answer.get("main")
        if main in topics:  # main topic first
            topics = [main] + [t for t in topics if t != main]
        if not topics:
            raise ValueError("choose at least one topic")
        row = {
            "chunk_id": chunk_id,
            "topics": topics,
            "main_topic": main if main in topics else None,
            "issue": (answer.get("issue") or "").strip() or None,
            "sentiment": answer.get("sentiment") or None,
            "stance": answer.get("stance") or None,
            "stance_target": (answer.get("stance_target") or "").strip() or None,
            "taxonomy_version": self.version,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with connect() as conn:
            conn.execute(
                """insert into text.topic_labels (chunk_id, source, topics, model, version)
                   values (%s, 'user', %s, 'human', %s)
                   on conflict (chunk_id, source) do update set topics = excluded.topics""",
                (chunk_id, topics, self.version),
            )
        self.done.add(chunk_id)

    def skip(self, chunk_id: int) -> None:
        self.skipped.add(int(chunk_id))


def handler(session: Session) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def reply(self, body: bytes, kind: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def json(self, data: Any, status: int = 200) -> None:
            self.reply(json.dumps(data, ensure_ascii=False).encode(), "application/json", status)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/next":
                self.json(session.next())
            else:
                self.reply(PAGE.read_bytes(), "text/html")

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            try:
                if self.path == "/api/save":
                    session.save(body)
                elif self.path == "/api/skip":
                    session.skip(body["chunk_id"])
            except ValueError as e:
                return self.json({"error": str(e)}, 400)
            self.json(session.next())

        def log_message(self, *args: Any) -> None:  # keep the terminal quiet
            pass

    return Handler


def serve(knessets: list[int] | None, n: int = 150, port: int = PORT, open_browser=True) -> None:
    session = Session(knessets, n)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(session))
    url = f"http://localhost:{port}"
    print(f"{len(session.done)}/{n} labeled. Labeling page: {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{len(session.done)}/{n} labeled.")
