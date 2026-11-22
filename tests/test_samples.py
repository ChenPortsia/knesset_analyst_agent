from knesset.db import connect
from knesset.samples import export


def test_export(db, tmp_path):
    with connect() as conn:
        for i, text in enumerate(["<< דובר >> משה כהן: << דובר >>\nשלום. " * 3, "א" * 9000]):
            conn.execute(
                """insert into text.documents (doc_id, source_table, parent_type, group_type_id,
                       group_type, url, status, knesset_num, text, n_chars)
                   values (%s, 'x', 'committee_session', 23, 'פרוטוקול ועדה', 'u', 'done', 25,
                           %s, %s)""",
                (f"committee_session:{i}", text, len(text)),
            )
    assert export([25], per_type=3, out=tmp_path) == 3
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files[0] == "_tags.tsv" and len(files) == 3
    long = (tmp_path / "committee_session__23__committee_session_1.txt").read_text()
    assert "characters omitted" in long and long.startswith("doc_id: committee_session:1")
    assert "committee_session\tפרוטוקול ועדה\tדובר\t6" in (tmp_path / "_tags.tsv").read_text()
