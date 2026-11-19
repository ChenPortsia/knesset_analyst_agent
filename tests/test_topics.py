from knesset.db import connect
from knesset.enrich.topics import load_topics, read_taxonomy


def test_taxonomy_file_is_valid():
    rows = read_taxonomy()
    parents = {r["topic_id"] for r in rows if r["parent_id"] is None}
    children = [r for r in rows if r["parent_id"] is not None]
    assert 30 <= len(children) <= 60  # PLAN: ~30-60 topics in two levels
    assert all(r["parent_id"] in parents for r in children)
    assert all(r["name_he"] and r["name_en"] for r in rows)


def test_load_topics(db, tmp_path):
    assert load_topics() == len(read_taxonomy())
    small = tmp_path / "t.yaml"
    small.write_text(
        "- {id: 10, he: משפט, en: Law, topics: [{id: 11, he: חוקה, en: Constitution}]}"
    )
    assert load_topics(small) == 2  # topics removed from the file are dropped
    with connect() as conn:
        rows = conn.execute("select topic_id, parent_id from text.topics order by 1").fetchall()
    assert rows == [(10, None), (11, 10)]
