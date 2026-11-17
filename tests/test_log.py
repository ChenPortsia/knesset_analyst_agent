import json
import os
import time

import pytest

from knesset import log
from knesset.config import settings


def read_lines(step: str) -> list[dict]:
    files = list((settings().log_dir / step).glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text().splitlines()]


def test_run_writes_json_lines_with_run_id():
    log.configure()
    with log.run("demo", record=False, knessets=[25]) as r:
        log.log.info("item_done", item="doc-1")
        r.rows_out = 3
    lines = read_lines("demo")
    assert [x["event"] for x in lines] == ["step_started", "item_done", "step_finished"]
    assert {x["run_id"] for x in lines} == {r.run_id}
    assert lines[1]["item"] == "doc-1"
    assert lines[2]["status"] == "ok" and lines[2]["rows_out"] == 3


def test_run_logs_failure_and_reraises():
    log.configure()
    with pytest.raises(ValueError), log.run("boom", record=False):
        raise ValueError("bad")
    assert read_lines("boom")[-1]["status"] == "failed"


def test_prune_removes_old_files(tmp_path):
    old, new = tmp_path / "s" / "old.jsonl", tmp_path / "s" / "new.jsonl"
    old.parent.mkdir()
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (time.time() - 100 * 86400,) * 2)
    log.prune(tmp_path, days=90)
    assert not old.exists() and new.exists()
