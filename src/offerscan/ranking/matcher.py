from __future__ import annotations

import re
from typing import Any

from offerscan.ai.evaluate import Engine, EvalStats, match_offers
from offerscan.models import Bucket, JobOffer, JuniorProfile, Match, WorkMode
from offerscan.pipeline.normalize import strip_accents
from offerscan.ranking.offer_score import OfferScore
from offerscan.settings import ScoringConfig
from offerscan.storage.db import Database

GENERIC_TERMS = {"git", "html", "css"}  # too common to signal a match on their own
PLATFORM_TERMS = [
    "java", "c++", "c#", ".net", "php", "ruby", "go", "golang", "kotlin", "swift", "cobol", "abap", "sap",
    "scala", "pl/sql", "plsql", "perl", "delphi", "rust", "angular", "salesforce", "mainframe",
]  # fmt: skip
DEV_TITLE = re.compile(
    r"desarrollad|programad|developer|engineer|ingenier|software|backend|back-end|frontend|front-end|"
    r"full ?stack|devops|\bdata\b|datos|analista|\bqa\b|tester|\bsre\b|cloud|\bia\b|\bai\b|\bml\b|"
    r"machine learning|python|react|web|mobile|android|ios|practicas|becari|trainee|junior"
)
_ENGLISH_REQ = re.compile(r"ingles (fluido|avanzado|alto|nativo)|fluent english|c1|c2|native english")


def _words(text: str) -> str:
    return strip_accents(text).lower()


def _found(term: str, haystack: str) -> bool:
    return bool(re.search(rf"(?<![\w+#]){re.escape(_words(term))}(?![\w+#])", haystack))


def skill_overlap(offer: JobOffer, junior: JuniorProfile) -> tuple[list[str], list[str]]:
    """(primary, secondary) matched terms.

    Primary: junior.stack terms found in the offer title or its own skills tags.
    Secondary: stack/nice_to_have terms only found in the description (never enough on their own).
    """
    head = _words(f"{offer.title} {' '.join(offer.skills)}")
    body = _words(offer.description_md)
    usable = lambda terms: [x for x in terms if x.lower() not in GENERIC_TERMS]  # noqa: E731
    primary = [x for x in usable(junior.stack) if _found(x, head)]
    secondary = [
        x for x in usable([*junior.stack, *junior.nice_to_have]) if x not in primary and _found(x, head + " " + body)
    ]
    return primary, secondary


def foreign_platform(offer: JobOffer, junior: JuniorProfile) -> str | None:
    """A language/platform named in the title that the junior does not use (and no stack term rescues it)."""
    title = _words(offer.title)
    named = [x for x in PLATFORM_TERMS if _found(x, title)]
    if not named:
        return None
    # Tags often list secondary tech, so only the title can rescue an offer for another platform.
    if any(_found(x, title) for x in junior.stack):
        return None
    return named[0]


def junior_prefilter(offer: JobOffer, junior: JuniorProfile) -> str | None:
    """Deterministic fit check run before spending credits. Returns the rejection reason."""
    if not DEV_TITLE.search(_words(offer.title)):
        return "el titulo no parece un puesto de desarrollo"
    if offer.work_mode is not WorkMode.REMOTE:
        place = _words(offer.location)
        if junior.locations and place and not any(_words(loc) in place for loc in junior.locations):
            return f"ubicacion fuera de las preferidas ({offer.location})"
        modes = junior.work_mode_pref
        if modes and offer.work_mode is not WorkMode.UNKNOWN and offer.work_mode not in modes:
            return f"modalidad {offer.work_mode.value} no deseada"
    if junior.min_salary and offer.salary_max and offer.salary_max < junior.min_salary:
        return "salario por debajo del minimo"
    if "en" not in [lang.lower() for lang in junior.languages] and _ENGLISH_REQ.search(
        _words(offer.description_md)
    ):
        return "exige ingles avanzado"
    return None


def select_candidates(
    ranked: list[tuple[JobOffer, OfferScore]], junior: JuniorProfile, top_n: int
) -> list[tuple[JobOffer, OfferScore, list[str], list[str]]]:
    picked = []
    for offer, score in ranked:
        if score.bucket is Bucket.DISCARD or junior_prefilter(offer, junior) or foreign_platform(offer, junior):
            continue
        primary, secondary = skill_overlap(offer, junior)
        if primary:
            picked.append((offer, score, primary, secondary))
    picked.sort(key=lambda t: (len(t[2]), len(t[3]), t[1].score), reverse=True)
    return picked[:top_n]


def _reasons(
    offer: JobOffer,
    junior: JuniorProfile,
    offer_score: OfferScore,
    overlap: tuple[list[str], list[str]],
    answers: dict[str, Any],
) -> list[str]:
    reasons = []
    primary, secondary = overlap
    if primary:
        reasons.append("Coincide en: " + ", ".join(primary[:6]))
    if secondary:
        reasons.append("Tambien en la descripcion: " + ", ".join(secondary[:4]))
    a = offer_score.answers
    if a.get("seniority", {}).get("choice") in ("junior", "internship"):
        reasons.append("Oferta pensada para perfiles junior")
    if a.get("mentorship", {}).get("noul", 0) >= 0.5:
        reasons.append("Menciona formacion o mentoring")
    if offer.work_mode is WorkMode.REMOTE:
        reasons.append("Trabajo remoto")
    elif offer.work_mode in junior.work_mode_pref:
        reasons.append(f"Modalidad {offer.work_mode.value} acorde a tus preferencias")
    if offer.has_salary:
        reasons.append(f"Salario visible ({offer.salary_min or offer.salary_max} EUR/anio)")
    if a.get("red_flags", {}).get("noul", 0) >= 0.4:
        reasons.append("Atencion: posibles senales de alarma")
    if answers.get("stack_match", {}).get("noul", 0) < 0.4:
        reasons.append("El stack principal cubre poco de lo pedido")
    reasons.extend(offer_score.notes)
    return reasons


def build_match(
    offer: JobOffer,
    junior: JuniorProfile,
    offer_score: OfferScore,
    overlap: tuple[list[str], list[str]],
    answers: dict[str, Any],
    cfg: ScoringConfig,
) -> Match:
    stack = float(answers["stack_match"]["noul"])
    fit_index = float(answers["junior_fit"].get("index", 0))
    fit = 100 * (0.7 * fit_index / 3 + 0.3 * stack)
    m = cfg.match
    final = m.offer_weight * offer_score.score + m.fit_weight * fit
    low_conf = (answers["junior_fit"].get("confidence") or 1.0) < cfg.low_confidence_threshold
    if offer_score.bucket is Bucket.REVIEW or low_conf:
        bucket = Bucket.REVIEW
    elif final >= m.top_threshold:
        bucket = Bucket.TOP
    elif final >= m.review_threshold:
        bucket = Bucket.REVIEW
    else:
        bucket = Bucket.DISCARD
    return Match(
        offer_id=offer.id,
        junior_slug=junior.slug,
        fit_score=round(fit, 1),
        stack_match=round(stack, 3),
        final_score=round(final, 1),
        bucket=bucket,
        reasons=_reasons(offer, junior, offer_score, overlap, answers),
    )


async def match_junior(
    db: Database,
    engine: Engine,
    junior: JuniorProfile,
    ranked: list[tuple[JobOffer, OfferScore]],
    cfg: ScoringConfig,
) -> tuple[list[Match], EvalStats]:
    candidates = select_candidates(ranked, junior, cfg.match.top_n)
    decisions, stats = await match_offers(db, engine, junior, [c[0] for c in candidates])
    matches = [
        build_match(offer, junior, score, (primary, secondary), decisions[offer.id].answers, cfg)
        for offer, score, primary, secondary in candidates
        if offer.id in decisions
    ]
    matches.sort(key=lambda m: m.final_score, reverse=True)
    db.clear_matches(junior.slug)
    for m in matches:
        db.save_match(m)
    return matches, stats
