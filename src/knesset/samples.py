"""Export sample documents per document type, and the markup tag inventory, for reading."""

import re
from pathlib import Path

from knesset.db import connect

OUT = Path(__file__).resolve().parents[2] / "eval" / "samples"
TAG = r"<<\s*([^<>]+?)\s*>>"


def export(
    knessets: list[int] | None,
    per_type: int = 3,
    types: int = 15,
    head: int = 6000,
    tail: int = 1500,
    out: Path = OUT,
) -> int:
    """Write `per_type` random extracted documents for each of the `types` most common
    (parent type, document type) pairs, plus `_tags.tsv`. Returns the number of files."""
    out.mkdir(parents=True, exist_ok=True)
    scope = "(%(k)s::int[] is null or knesset_num = any(%(k)s))"
    with connect() as conn:
        rows = conn.execute(
            f"""with kinds as (
                    select parent_type, group_type_id, count(*) n from text.documents
                    where status = 'done' and {scope} group by 1, 2
                    order by 3 desc limit %(types)s),
                picks as (
                    select d.*, row_number() over (partition by d.parent_type, d.group_type_id
                                                   order by random()) rn
                    from text.documents d join kinds using (parent_type, group_type_id)
                    where d.status = 'done' and {scope})
                select doc_id, parent_type, group_type_id, group_type, knesset_num,
                       doc_date::date, url, n_chars, text
                from picks where rn <= %(per)s order by parent_type, group_type_id""",
            {"k": knessets, "types": types, "per": per_type},
        ).fetchall()
        tags = conn.execute(
            f"""select parent_type, group_type, m[1] as tag, count(*)
                from text.documents, regexp_matches(text, %(tag)s, 'g') as m
                where status = 'done' and {scope}
                group by 1, 2, 3 having count(*) >= 5 order by 1, 2, 4 desc""",
            {"k": knessets, "tag": TAG},
        ).fetchall()
    for doc_id, ptype, gid, gtype, knesset, date, url, n_chars, text in rows:
        body = (
            text
            if len(text) <= head + tail
            else (
                f"{text[:head]}\n\n[… {len(text) - head - tail:,} characters omitted …]\n\n"
                f"{text[-tail:]}"
            )
        )
        name = re.sub(r"[^\w]+", "_", f"{ptype}__{gid}__{doc_id}")
        (out / f"{name}.txt").write_text(
            f"doc_id: {doc_id}\nparent_type: {ptype}\ngroup_type: {gid} {gtype}\n"
            f"knesset: {knesset}\ndate: {date}\nurl: {url}\nchars: {n_chars:,}\n\n{body}\n",
            encoding="utf-8",
        )
    with (out / "_tags.tsv").open("w", encoding="utf-8") as f:
        f.write("parent_type\tgroup_type\ttag\tcount\n")
        for row in tags:
            f.write("\t".join(map(str, row)) + "\n")
    return len(rows) + 1
