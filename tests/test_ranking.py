import asyncio
from datetime import datetime, timedelta, timezone

from offerscan.ai.evaluate import evaluate_offers
from offerscan.ai.heuristic import HeuristicEngine
from offerscan.models import Bucket, JobOffer, JuniorProfile, OfferDecision, WorkMode, make_offer_id
from offerscan.ranking.matcher import foreign_platform, junior_prefilter, match_junior, skill_overlap
from offerscan.ranking.offer_score import rank_offers, score_offer
from offerscan.settings import ScoringConfig
from offerscan.storage.db import STATUS_CANDIDATE, Database

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
CFG = ScoringConfig()


LONG_DESC = "Buscamos a alguien con ganas de aprender. " * 8


def offer(ext, title="Junior Python Developer", desc=LONG_DESC, **kw) -> JobOffer:
    return JobOffer(
        id=make_offer_id("t", ext), source="t", external_id=ext, url="u", title=title, company="c",
        scraped_at=NOW, description_md=desc, **kw,
    )  # fmt: skip


def decision(o, **answers) -> OfferDecision:
    base = {
        "seniority": {"type": "choice", "choice": "junior", "confidence": 0.9},
        "experience_required": {"type": "score", "index": 1.0, "confidence": 0.9},
        "junior_friendly": {"type": "noul", "noul": 0.9},
        "mentorship": {"type": "noul", "noul": 0.6},
        "red_flags": {"type": "noul", "noul": 0.05},
        "offer_quality": {"type": "score", "index": 2.0, "confidence": 0.8},
    }
    base.update(answers)
    return OfferDecision(offer_id=o.id, question_set_version="offer-v1", model="m", answers=base,
                         created_at=NOW, engine="jev")  # fmt: skip


def test_score_ordering_and_range():
    good = offer("1", salary_min=24000, published_at=NOW - timedelta(days=1), work_mode=WorkMode.REMOTE)
    bad = offer("2", title="Senior")
    bad_dec = decision(
        bad,
        seniority={"type": "choice", "choice": "senior", "confidence": 0.9},
        junior_friendly={"type": "noul", "noul": 0.05},
        experience_required={"type": "score", "index": 4.0, "confidence": 0.9},
    )
    s_good, s_bad = score_offer(good, decision(good), CFG, NOW), score_offer(bad, bad_dec, CFG, NOW)
    assert 0 <= s_bad.score < s_good.score <= 100
    assert s_good.bucket is Bucket.TOP


def test_red_flags_discard_and_low_confidence_review():
    o = offer("1")
    flagged = score_offer(o, decision(o, red_flags={"type": "noul", "noul": 0.8}), CFG, NOW)
    assert flagged.bucket is Bucket.DISCARD
    unsure = score_offer(
        o, decision(o, seniority={"type": "choice", "choice": "junior", "confidence": 0.2}), CFG, NOW
    )
    assert unsure.bucket is Bucket.REVIEW and "seniority" in unsure.notes[0]


def test_confident_senior_discarded_and_mid_reviewed():
    o = offer("1")
    senior = decision(o, seniority={"type": "choice", "choice": "senior", "confidence": 0.9})
    assert score_offer(o, senior, CFG, NOW).bucket is Bucket.DISCARD
    mid = decision(o, seniority={"type": "choice", "choice": "mid", "confidence": 0.9})
    assert score_offer(o, mid, CFG, NOW).bucket is Bucket.REVIEW
    unsure_senior = decision(o, seniority={"type": "choice", "choice": "senior", "confidence": 0.2})
    assert score_offer(o, unsure_senior, CFG, NOW).bucket is Bucket.REVIEW


def test_missing_confidence_does_not_force_review():
    o = offer("1")
    d = decision(o, seniority={"type": "choice", "choice": "junior", "confidence": None})
    assert score_offer(o, d, CFG, NOW).bucket is Bucket.TOP


ANA = JuniorProfile(slug="ana", stack=["python", "django"], locations=["Madrid"],
                    work_mode_pref=[WorkMode.HYBRID, WorkMode.REMOTE], languages=["es"], min_salary=20000)  # fmt: skip


def test_junior_prefilter_rules():
    assert junior_prefilter(offer("1", location="Madrid", work_mode=WorkMode.HYBRID), ANA) is None
    assert "ubicacion" in junior_prefilter(offer("2", location="Sevilla", work_mode=WorkMode.HYBRID), ANA)
    assert "modalidad" in junior_prefilter(offer("3", location="Madrid", work_mode=WorkMode.ONSITE), ANA)
    assert junior_prefilter(offer("4", location="Sevilla", work_mode=WorkMode.REMOTE), ANA) is None
    assert "salario" in junior_prefilter(offer("5", location="Madrid", salary_max=15000), ANA)
    assert "ingles" in junior_prefilter(offer("6", desc="Ingles fluido", location="Madrid"), ANA)


def test_non_dev_titles_rejected():
    for title in ["Técnico de mantenimiento electrónico", "STEM Teacher & Coordinator", "Comercial de ventas"]:
        assert "no parece" in junior_prefilter(offer("x", title=title, location="Madrid"), ANA)
    assert junior_prefilter(offer("y", title="Desarrollador Python", location="Madrid", work_mode=WorkMode.HYBRID), ANA) is None


def test_skill_overlap_primary_needs_stack_in_title_or_skills():
    o = offer("1", title="Dev", desc="Experiencia con Django y pythonic code", skills=["python"])
    assert skill_overlap(o, ANA) == (["python"], ["django"])
    only_desc = offer("2", title="Dev", desc="usamos python, django y docker")
    primary, secondary = skill_overlap(only_desc, ANA)
    assert primary == [] and "python" in secondary
    assert skill_overlap(offer("3", title="Dev", desc="javascript"), ANA) == ([], [])
    nice = JuniorProfile(slug="x", stack=["python"], nice_to_have=["aws"])
    assert skill_overlap(offer("4", title="Dev", skills=["aws"]), nice)[0] == []


def test_foreign_platform_excluded():
    assert foreign_platform(offer("1", title="Programadores Java", skills=["java"]), ANA) == "java"
    assert foreign_platform(offer("2", title="Desarrollador C++", skills=["c++", "python"]), ANA) == "c++"
    assert foreign_platform(offer("3", title="Java y Python Developer"), ANA) is None
    assert foreign_platform(offer("4", title="Junior Backend Developer"), ANA) is None
    assert foreign_platform(offer("5", title="Junior C# Developer"), ANA) == "c#"


def test_end_to_end_match_with_heuristic():
    db = Database(":memory:")
    offers = [
        offer("1", desc="Python y Django, mentoring y formacion", location="Madrid",
              work_mode=WorkMode.HYBRID, skills=["python", "django"]),
        offer("2", title="Junior Cobol", desc="mainframe", location="Madrid", work_mode=WorkMode.HYBRID),
        offer("3", desc="Python", location="Sevilla", work_mode=WorkMode.ONSITE),
    ]  # fmt: skip
    db.upsert_offers(offers)
    for o in offers:
        db.set_status(o.id, STATUS_CANDIDATE)
    engine = HeuristicEngine()

    async def go():
        await evaluate_offers(db, engine, offers)
        ranked = rank_offers(db, offers, CFG, NOW)
        return await match_junior(db, engine, ANA, ranked, CFG)

    matches, _ = asyncio.run(go())
    assert [m.offer_id for m in matches] == [offers[0].id]
    m = matches[0]
    assert 0 <= m.final_score <= 100 and m.reasons and m.reasons[0].startswith("Coincide en: python")
    assert db.matches_for("ana")[0].offer_id == offers[0].id
