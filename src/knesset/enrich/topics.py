"""The topic taxonomy (topics.yaml) → text.topics."""

from pathlib import Path

import yaml

from knesset.db import connect, migrate

TAXONOMY = Path(__file__).with_name("topics.yaml")


def read_taxonomy(path: Path = TAXONOMY) -> list[dict]:
    """Flat rows: parents first, then their topics with parent_id set."""
    rows = []
    for parent in yaml.safe_load(path.read_text(encoding="utf-8")):
        rows.append(
            {
                "topic_id": parent["id"],
                "name_he": parent["he"],
                "name_en": parent["en"],
                "description": None,
                "parent_id": None,
            }
        )
        for t in parent["topics"]:
            rows.append(
                {
                    "topic_id": t["id"],
                    "name_he": t["he"],
                    "name_en": t["en"],
                    "description": t.get("covers"),
                    "parent_id": parent["id"],
                }
            )
    ids = [r["topic_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate topic ids in {path.name}")
    return rows


def load_topics(path: Path = TAXONOMY) -> int:
    """Upsert the taxonomy and drop topics removed from the file. Returns the topic count."""
    migrate()
    rows = read_taxonomy(path)
    with connect() as conn:
        conn.execute(
            "delete from text.topics where not (topic_id = any(%s))",
            ([r["topic_id"] for r in rows],),
        )
        conn.cursor().executemany(
            """insert into text.topics (topic_id, name_he, name_en, description, parent_id)
               values (%(topic_id)s, %(name_he)s, %(name_en)s, %(description)s, %(parent_id)s)
               on conflict (topic_id) do update set name_he = excluded.name_he,
                   name_en = excluded.name_en, description = excluded.description,
                   parent_id = excluded.parent_id""",
            rows,
        )
    return len(rows)
