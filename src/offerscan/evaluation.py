"""Calibration of `junior_friendly` against hand-labeled offers (tests/eval/labeled_offers.jsonl)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from offerscan.ai.questions import OFFER_QUESTIONS_V1, build_offer_state
from offerscan.models import OfferDecision
from offerscan.pipeline.normalize import normalize_raw
from offerscan.ranking.offer_score import _junior_target_probs
from offerscan.service import make_engine
from offerscan.settings import Settings
from offerscan.storage.db import LABEL_JUNIOR, Database

MAX_EXPORT_CHARS = 1500
THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
MIN_PRECISION = 0.8
MIN_LABELS = 30
MIN_PER_CLASS = 8


@dataclass
class ThresholdRow:
    threshold: float
    precision: float
    recall: float
    f1: float
    predicted: int


@dataclass
class EvalReport:
    total: int
    positives: int
    thresholds: list[ThresholdRow]


def score_thresholds(scored: list[tuple[float, bool]]) -> list[ThresholdRow]:
    positives = sum(label for _, label in scored)
    rows = []
    for th in THRESHOLDS:
        predicted = [(s, y) for s, y in scored if s >= th]
        tp = sum(y for _, y in predicted)
        precision = tp / len(predicted) if predicted else 0.0
        recall = tp / positives if positives else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        rows.append(ThresholdRow(th, precision, recall, f1, len(predicted)))
    return rows


def read_labeled(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def labeled_rows_from_db(db: Database) -> list[dict]:
    """Human labels stored by the UI, in the tests/eval/labeled_offers.jsonl row format."""
    rows = []
    for offer_id, label in db.labels().items():
        offer = db.get_offer(offer_id)
        if offer is None:
            continue
        rows.append(
            {
                "origin": f"{offer.source} (etiqueta humana desde la UI)",
                "title": offer.title,
                "company": offer.company,
                "description": offer.description_md[:MAX_EXPORT_CHARS],
                "label": label == LABEL_JUNIOR,
            }
        )
    return rows


def export_labeled_offers(db: Database, path: Path) -> tuple[int, int]:
    """Append DB labels to a JSONL file, skipping (title, company) pairs already there. Returns (added, total)."""
    existing = read_labeled(path) if path.exists() else []
    seen = {(r["title"], r.get("company", "")) for r in existing}
    new = [r for r in labeled_rows_from_db(db) if (r["title"], r["company"]) not in seen]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in new:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(new), len(existing) + len(new)


def run_eval(path: Path, settings: Settings, engine_name: str) -> EvalReport:
    return run_eval_rows(read_labeled(path), settings, engine_name)


def run_eval_rows(lines: list[dict], settings: Settings, engine_name: str) -> EvalReport:
    now = datetime.now(timezone.utc)
    only = {"junior_friendly": OFFER_QUESTIONS_V1["junior_friendly"]}

    async def go() -> list[tuple[float, bool]]:
        engine = make_engine(engine_name, settings)
        try:
            out = []
            for i, row in enumerate(lines):
                offer = normalize_raw(
                    {"source": "eval", "external_id": str(i), "url": "", "title": row["title"],
                     "company": row.get("company", ""), "description": row.get("description", "")},
                    now,
                )  # fmt: skip
                result = await engine.ask(build_offer_state(offer), only)
                out.append((result.answers["junior_friendly"]["noul"], bool(row["label"])))
            return out
        finally:
            await engine.aclose()

    scored = asyncio.run(go())
    return EvalReport(len(scored), sum(y for _, y in scored), score_thresholds(scored))


# ---------------------------------------------------------------------------------------------
# Calibration against the human labels stored in the DB (decisions are reused, no engine calls).
# ---------------------------------------------------------------------------------------------


@dataclass
class ScoredLabel:
    offer_id: str
    signal: float
    label: bool


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0


@dataclass
class EngineCalibration:
    engine: str
    labeled: int
    positives: int
    rows: list[ThresholdRow]
    recommended: float | None
    precision_ok: bool  # recommended threshold reaches MIN_PRECISION
    selected: float
    confusion: Confusion
    false_positives: list[ScoredLabel]
    false_negatives: list[ScoredLabel]

    @property
    def negatives(self) -> int:
        return self.labeled - self.positives

    @property
    def few_labels(self) -> bool:
        return self.labeled < MIN_LABELS or min(self.positives, self.negatives) < MIN_PER_CLASS


def junior_signal(decision: OfferDecision) -> float | None:
    """P(junior) as the bucket rules see it: `junior_target` first, `junior_friendly` as fallback."""
    target = _junior_target_probs(decision.answers)
    if target is not None:
        return target[0]
    friendly = decision.answers.get("junior_friendly")
    return float(friendly["noul"]) if friendly else None


def recommend_threshold(rows: list[ThresholdRow]) -> tuple[float | None, bool]:
    """Best F1 among thresholds with precision >= MIN_PRECISION; otherwise best F1 overall (flagged)."""
    usable = [r for r in rows if r.predicted]
    if not usable:
        return None, False
    good = [r for r in usable if r.precision >= MIN_PRECISION]
    pool = good or usable
    best = max(pool, key=lambda r: (r.f1, r.threshold))
    return best.threshold, bool(good)


def confusion_at(scored: list[ScoredLabel], threshold: float) -> Confusion:
    c = Confusion()
    for item in scored:
        predicted = item.signal >= threshold
        if predicted and item.label:
            c.tp += 1
        elif predicted:
            c.fp += 1
        elif item.label:
            c.fn += 1
        else:
            c.tn += 1
    return c


def calibrate(
    scored_by_engine: dict[str, list[ScoredLabel]], thresholds: dict[str, float | None] | None = None
) -> list[EngineCalibration]:
    out = []
    for engine, scored in sorted(scored_by_engine.items()):
        rows = score_thresholds([(s.signal, s.label) for s in scored])
        recommended, ok = recommend_threshold(rows)
        selected = (thresholds or {}).get(engine) or recommended or 0.5
        out.append(
            EngineCalibration(
                engine=engine,
                labeled=len(scored),
                positives=sum(s.label for s in scored),
                rows=rows,
                recommended=recommended,
                precision_ok=ok,
                selected=selected,
                confusion=confusion_at(scored, selected),
                false_positives=[s for s in scored if s.signal >= selected and not s.label],
                false_negatives=[s for s in scored if s.signal < selected and s.label],
            )
        )
    return out


def scored_labels_from_db(db: Database) -> dict[str, list[ScoredLabel]]:
    """Per engine: every labeled offer with a stored decision from that engine (latest decision wins)."""
    from offerscan.ai.questions import OFFER_VERSIONS

    labels = db.labels()
    latest: dict[tuple[str, str], OfferDecision] = {}
    for d in db.all_decisions():
        if d.offer_id in labels and d.question_set_version in OFFER_VERSIONS:
            key = (d.offer_id, d.engine)
            if key not in latest or d.created_at > latest[key].created_at:
                latest[key] = d
    out: dict[str, list[ScoredLabel]] = {}
    for (offer_id, engine), decision in latest.items():
        signal = junior_signal(decision)
        if signal is not None:
            out.setdefault(engine, []).append(ScoredLabel(offer_id, signal, labels[offer_id] == LABEL_JUNIOR))
    return out


def calibrate_db(db: Database, thresholds: dict[str, float | None] | None = None) -> list[EngineCalibration]:
    return calibrate(scored_labels_from_db(db), thresholds)
