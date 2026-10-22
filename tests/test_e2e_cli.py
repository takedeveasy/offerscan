import csv
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from offerscan.cli import app
from offerscan.demo import load_fixture_raws
from offerscan.evaluation import score_thresholds

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_load_fixture_raws_uses_real_parsers():
    raws = load_fixture_raws(ROOT / "tests" / "fixtures")
    assert {r["source"] for r in raws} == {"tecnoempleo", "linkedin", "infojobs", "demo"}
    assert len(raws) >= 50


def test_run_from_fixtures_end_to_end(env):
    result = runner.invoke(app, ["run", "--engine", "heuristic", "--from-fixtures", str(ROOT / "tests" / "fixtures")])
    assert result.exit_code == 0, result.output
    reports = env / "reports"
    for slug in ("backend-python-madrid", "frontend-react-remote"):
        for ext in ("html", "md", "csv", "jsonl"):
            assert (reports / f"{slug}.{ext}").stat().st_size > 0
    html = (reports / "backend-python-madrid.html").read_text(encoding="utf-8")
    assert "Junior Backend Developer Python" in html and "Para revisar a mano" in html
    rows = list(csv.DictReader((reports / "backend-python-madrid.csv").open(encoding="utf-8-sig")))
    assert rows and float(rows[0]["final_score"]) >= float(rows[-1]["final_score"])
    first = json.loads((reports / "backend-python-madrid.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["url"].startswith("http")

    # second run is idempotent: everything cached, nothing new
    again = runner.invoke(app, ["evaluate", "--engine", "heuristic"])
    assert again.exit_code == 0 and "0 nuevas" in again.output


def test_jev_requested_without_key_falls_back(env):
    result = runner.invoke(app, ["evaluate", "--engine", "jev"])
    assert result.exit_code == 0 and "heuristico" in result.output


def test_report_unknown_junior_fails_cleanly(env):
    result = runner.invoke(app, ["report", "--junior", "nadie"])
    assert result.exit_code == 1


def test_eval_command_and_thresholds(env):
    result = runner.invoke(app, ["eval", "--file", str(ROOT / "tests" / "eval" / "labeled_offers.jsonl")])
    assert result.exit_code == 0 and "Precision" in result.output
    rows = score_thresholds([(0.9, True), (0.8, True), (0.4, False), (0.2, True)])
    at_half = next(r for r in rows if r.threshold == 0.5)
    assert (at_half.precision, round(at_half.recall, 2)) == (1.0, 0.67)


def test_backend_top_bucket_has_no_foreign_or_senior_titles(env):
    result = runner.invoke(app, ["run", "--engine", "heuristic", "--from-fixtures", str(ROOT / "tests" / "fixtures")])
    assert result.exit_code == 0, result.output
    rows = [json.loads(x) for x in (env / "reports" / "backend-python-madrid.jsonl").read_text(encoding="utf-8").splitlines()]
    # fixture listings carry snippets (<200 chars), so offers cap at "review" (no description -> no top)
    top = [r for r in rows if r["bucket"] in ("top", "review")]
    assert top
    forbidden = re.compile(r"\b(java|c\+\+|\.net|c#|php|cobol|arquitect\w*|senior|sr|lead|l[ií]der)\b", re.I)
    for r in top:
        assert not forbidden.search(r["title"]) or re.search(r"python", r["title"], re.I), r["title"]
    assert not any(re.search(r"arquitect|senior|lead", r["title"], re.I) for r in top)
    for r in rows:
        assert r["reasons"][0].startswith("Coincide en:") or "Coincide en" in " ".join(r["reasons"])


def test_eval_from_db_labels_and_export(env):
    from offerscan.storage.db import Database

    empty = runner.invoke(app, ["eval", "--from-db-labels", "--engine", "heuristic"])
    assert empty.exit_code == 1 and "No hay etiquetas" in empty.output
    run = runner.invoke(app, ["run", "--engine", "heuristic", "--from-fixtures", str(ROOT / "tests" / "fixtures")])
    assert run.exit_code == 0, run.output
    db = Database(env / "data" / "offerscan.sqlite")
    offers = db.all_offers()[:2]
    db.set_label(offers[0].id, "junior")
    db.set_label(offers[1].id, "no_junior")
    result = runner.invoke(app, ["eval", "--from-db-labels", "--engine", "heuristic"])
    assert result.exit_code == 0 and "2 ofertas" in result.output.replace("\n", " ")
    out = env / "labels.jsonl"
    exported = runner.invoke(app, ["eval", "--export-labels", str(out)])
    assert exported.exit_code == 0 and len(out.read_text(encoding="utf-8").splitlines()) == 2
