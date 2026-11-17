from typer.testing import CliRunner

from knesset import odata, tracing
from knesset.cli import app


def test_status_reports_unreachable_services(monkeypatch):
    monkeypatch.setenv("DB_PORT", "1")
    monkeypatch.setattr(tracing, "setup", lambda: None)  # no exporter retries against a closed port
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 1
    assert "ERR  db" in result.output
    assert "ERR  phoenix" in result.output
    assert "ok   device" in result.output


def test_sync_reports_geo_block(monkeypatch):
    async def blocked(*args, **kwargs):
        raise odata.GeoBlockedError("knesset.gov.il answers Israeli IPs only")

    monkeypatch.setattr(tracing, "setup", lambda: None)
    monkeypatch.setattr("knesset.sync.sync", blocked)
    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 2
    assert "Israeli" in result.output
