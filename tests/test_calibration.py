from datetime import datetime, timezone
from pathlib import Path

import pytest

from offerscan.evaluation import (
    MIN_PRECISION,
    ScoredLabel,
    calibrate,
    calibrate_db,
    confusion_at,
    recommend_threshold,
    score_thresholds,
)
from offerscan.models import OfferDecision
from offerscan.pipeline.normalize import normalize_raw
from offerscan.service import apply_junior_threshold
from offerscan.storage.db import LABEL_JUNIOR, LABEL_NOT_JUNIOR, Database

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def seed(db: Database, engine: str, rows: list[tuple[float, bool]]) -> list[str]:
    ids = []
    for i, (p_junior, is_junior) in enumerate(rows):
        offer = normalize_raw(
            {"source": "tecnoempleo", "external_id": f"{engine}-{i}", "url": f"https://x.test/{engine}/{i}",
             "title": f"Dev {i}", "company": "ACME", "description": "d" * 300},
            NOW,
        )  # fmt: skip
        db.upsert_offers([offer])
        db.save_decision(
            OfferDecision(
                offer_id=offer.id,
                question_set_version="offer-v1",
                model=f"m-{engine}",
                answers={"junior_target": {"choice": "junior", "probabilities": {"junior": p_junior}}},
                created_at=NOW,
                engine=engine,
            )
        )
        db.set_label(offer.id, LABEL_JUNIOR if is_junior else LABEL_NOT_JUNIOR)
        ids.append(offer.id)
    return ids


def test_recommend_prefers_precision_then_f1():
    scored = [(0.95, True), (0.9, True), (0.85, True), (0.7, False), (0.65, True), (0.4, False), (0.35, False)]
    rows = score_thresholds(scored)
    best, ok = recommend_threshold(rows)
    assert ok and best == 0.6
    assert next(r for r in rows if r.threshold == best).precision >= MIN_PRECISION


def test_recommend_falls_back_when_precision_unreachable():
    rows = score_thresholds([(0.9, False), (0.8, True), (0.7, False), (0.6, True)])
    best, ok = recommend_threshold(rows)
    assert best is not None and not ok


def test_confusion_and_errors():
    scored = [ScoredLabel("a", 0.9, True), ScoredLabel("b", 0.8, False), ScoredLabel("c", 0.2, True),
              ScoredLabel("d", 0.1, False)]  # fmt: skip
    c = confusion_at(scored, 0.5)
    assert (c.tp, c.fp, c.fn, c.tn) == (1, 1, 1, 1)
    [cal] = calibrate({"heuristic": scored}, {"heuristic": 0.5})
    assert cal.few_labels and cal.labeled == 4
    assert [s.offer_id for s in cal.false_positives] == ["b"]
    assert [s.offer_id for s in cal.false_negatives] == ["c"]


def test_calibrate_db_groups_by_engine(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    seed(db, "heuristic", [(0.9, True), (0.8, True), (0.2, False), (0.7, False)])
    seed(db, "laya", [(0.95, True), (0.1, False)])
    engines = {e.engine: e for e in calibrate_db(db)}
    assert set(engines) == {"heuristic", "laya"}
    assert engines["heuristic"].labeled == 4 and engines["laya"].labeled == 2
    assert engines["laya"].few_labels


def test_apply_threshold_keeps_comments_and_makes_backup(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    original = (ROOT / "config" / "scoring.yaml").read_text(encoding="utf-8")
    (cfg / "scoring.yaml").write_text(original, encoding="utf-8")
    apply_junior_threshold(cfg, 0.7)
    new = (cfg / "scoring.yaml").read_text(encoding="utf-8")
    assert "junior_target_min: 0.7" in new and "# P(junior) below this" in new
    assert (cfg / "scoring.yaml.bak").read_text(encoding="utf-8") == original
    with pytest.raises(ValueError):
        apply_junior_threshold(cfg, 1.5)
