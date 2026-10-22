from typer.testing import CliRunner

from offerscan.cli import app


def test_doctor_runs_and_never_prints_key(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "sk_super_secret")
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "JEV_API_KEY" in result.output
    assert "sk_super_secret" not in result.output
