from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from offerscan.ai.questions import OFFER_VERSIONS
from offerscan.models import Bucket, JobOffer, OfferDecision, WorkMode
from offerscan.settings import ScoringConfig
from offerscan.storage.db import Database

DEFAULT_WEIGHTS = {
    "junior_friendly": 25,
    "low_red_flags": 20,
    "seniority": 15,
    "low_experience": 15,
    "offer_quality": 10,
    "mentorship": 5,
    "salary_visible": 4,
    "recency": 3,
    "work_mode": 3,
}
SENIORITY_VALUE = {"internship": 1.0, "junior": 1.0, "unclear": 0.4, "mid": 0.2, "senior": 0.0}
RECENCY_DAYS = 30


@dataclass
class OfferScore:
    offer_id: str
    score: float
    bucket: Bucket
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    answers: dict[str, Any] = field(default_factory=dict)


def latest_offer_decision(db: Database, offer_id: str) -> OfferDecision | None:
    decisions = [d for d in db.decisions_for(offer_id) if d.question_set_version in OFFER_VERSIONS]
    return max(decisions, key=lambda d: d.created_at, default=None)


def _noul(answers: dict[str, Any], key: str, default: float = 0.5) -> float:
    return float(answers.get(key, {}).get("noul", default))


def _index(answers: dict[str, Any], key: str, default: float) -> float:
    return float(answers.get(key, {}).get("index", default))


def _confidence(answers: dict[str, Any], key: str) -> float | None:
    return answers.get(key, {}).get("confidence")


def _junior_target_probs(answers: dict[str, Any]) -> tuple[float, float] | None:
    """(P(junior), P(con_experiencia)) from the `junior_target` answer, if the engine gave one."""
    target = answers.get("junior_target")
    if not target:
        return None
    probs = target.get("probabilities") or {}
    choice = target.get("choice")

    def prob(option: str) -> float:
        return float(probs.get(option, 1.0 if choice == option else 0.0))

    return prob("junior"), prob("con_experiencia")


def _bucket(
    offer: JobOffer, a: dict[str, Any], cfg: ScoringConfig, red_flags: float, seniority: str
) -> tuple[Bucket, list[str]]:
    """Discard rules first, then review rules; anything left is top."""
    seniority_confident = (_confidence(a, "seniority") or 0.0) >= cfg.low_confidence_threshold
    target = _junior_target_probs(a)
    salary = offer.salary_min or offer.salary_max
    if red_flags > cfg.red_flags_discard:
        return Bucket.DISCARD, ["senales de alarma altas"]
    if salary and offer.salary_min and salary > cfg.junior_salary_max:
        return Bucket.DISCARD, [f"salario de perfil con experiencia (desde {salary} EUR/anio)"]
    if seniority == "senior" and seniority_confident:
        return Bucket.DISCARD, ["perfil senior"]
    if target and target[1] >= cfg.junior_target_discard:
        return Bucket.DISCARD, ["pensada para perfiles con experiencia (junior_target)"]

    notes: list[str] = []
    if seniority == "mid" and seniority_confident:
        notes.append("pide perfil con experiencia (mid)")
    if target and (a["junior_target"].get("choice") == "no_claro" or target[0] < cfg.junior_target_min):
        notes.append("no esta claro que sea para junior")
    if salary and offer.salary_min and salary > cfg.junior_salary_max * cfg.junior_salary_review_ratio:
        notes.append(f"salario cercano al tope junior ({salary} EUR/anio)")
    if len(offer.description_md.strip()) < cfg.min_description_chars:
        notes.append("sin descripcion, revisar en el portal")
    low = [
        k
        for k in ("seniority", "experience_required")
        if (c := _confidence(a, k)) is not None and c < cfg.low_confidence_threshold
    ]
    if low:
        notes.append("baja confianza en: " + ", ".join(low))
    return (Bucket.REVIEW if notes else Bucket.TOP), notes


def score_offer(
    offer: JobOffer,
    decision: OfferDecision,
    cfg: ScoringConfig,
    now: datetime | None = None,
) -> OfferScore:
    now = now or datetime.now(timezone.utc)
    a = decision.answers
    weights = {**DEFAULT_WEIGHTS, **cfg.weights}
    red_flags = _noul(a, "red_flags", 0.0)
    seniority = a.get("seniority", {}).get("choice", "unclear")
    age_days = None
    if offer.published_at:
        published = offer.published_at
        published = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
        age_days = max((now - published).days, 0)
    components = {
        "junior_friendly": _noul(a, "junior_friendly"),
        "low_red_flags": 1 - red_flags,
        "seniority": SENIORITY_VALUE.get(seniority, 0.4),
        "low_experience": 1 - _index(a, "experience_required", 1.0) / 4,
        "offer_quality": _index(a, "offer_quality", 1.0) / 3,
        "mentorship": _noul(a, "mentorship", 0.0),
        "salary_visible": 1.0 if offer.has_salary else 0.0,
        "recency": 0.5 if age_days is None else max(0.0, 1 - age_days / RECENCY_DAYS),
        "work_mode": {WorkMode.REMOTE: 1.0, WorkMode.HYBRID: 0.8}.get(offer.work_mode, 0.5),
    }
    total = sum(weights[k] for k in components)
    score = 100 * sum(weights[k] * v for k, v in components.items()) / total if total else 0.0

    bucket, notes = _bucket(offer, a, cfg, red_flags, seniority)
    return OfferScore(offer.id, round(score, 1), bucket, components, notes, a)


def rank_offers(
    db: Database, offers: list[JobOffer], cfg: ScoringConfig, now: datetime | None = None
) -> list[tuple[JobOffer, OfferScore]]:
    ranked = []
    for offer in offers:
        decision = latest_offer_decision(db, offer.id)
        if decision is not None:
            ranked.append((offer, score_offer(offer, decision, cfg, now)))
    ranked.sort(key=lambda pair: pair[1].score, reverse=True)
    return ranked
