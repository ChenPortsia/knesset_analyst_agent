from typer.testing import CliRunner

from knesset import tracing
from knesset.cli import app


def test_status_reports_unreachable_services(monkeypatch):
    monkeypatch.setattr(tracing, "setup", lambda: None)  # no exporter retries against a closed port
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 1
    assert "ERR  db" in result.output
    assert "ERR  phoenix" in result.output
    assert "ok   device" in result.output
