"""Regression tests for the junior gating, built from real false positives (no DB needed)."""

from datetime import datetime, timezone

import pytest

from offerscan.models import Bucket, JobOffer, OfferDecision, make_offer_id
from offerscan.pipeline.prefilter import prefilter
from offerscan.ranking.offer_score import score_offer
from offerscan.settings import PrefilterConfig, ScoringConfig, SearchConfig, load_search_config

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
CFG = ScoringConfig()
PRE = PrefilterConfig()
LONG = "Descripcion de la oferta con funciones y requisitos. " * 6


def make_offer(title, source="infojobs", desc="", **kw) -> JobOffer:
    return JobOffer(
        id=make_offer_id(source, title), source=source, external_id=title, url="u", title=title,
        company="c", scraped_at=NOW, description_md=desc, **kw,
    )  # fmt: skip


def laya(junior=0.2, exp=0.6, unclear=0.2, seniority="junior", sen_conf=0.78) -> OfferDecision:
    probs = {"junior": junior, "con_experiencia": exp, "no_claro": unclear}
    choice = max(probs, key=probs.get)
    answers = {
        "seniority": {"type": "choice", "choice": seniority, "confidence": sen_conf},
        "junior_target": {"type": "choice", "choice": choice, "probabilities": probs, "confidence": probs[choice]},
        "experience_required": {"type": "score", "index": 1.0, "confidence": 0.8},
        "junior_friendly": {"type": "noul", "noul": junior},
        "red_flags": {"type": "noul", "noul": 0.05},
        "mentorship": {"type": "noul", "noul": 0.1},
        "offer_quality": {"type": "score", "index": 1.0, "confidence": 0.5},
    }
    return OfferDecision(offer_id="x", question_set_version="offer-laya-v1", model="laya:m", answers=answers,
                         created_at=NOW, engine="laya")  # fmt: skip


def bucket(offer, decision) -> Bucket:
    return score_offer(offer, decision, CFG, NOW).bucket


def test_melt_frontend_react_is_no_longer_top():
    melt = make_offer("Frontend Developer (React) - Remoto", salary_min=45000, salary_max=66000)
    result = score_offer(melt, laya(junior=0.25, exp=0.63, unclear=0.12), CFG, NOW)
    assert result.bucket is Bucket.DISCARD


def test_junior_target_alone_discards_even_with_good_salary_and_description():
    o = make_offer("Frontend Developer (React)", desc=LONG, salary_min=24000)
    assert bucket(o, laya(junior=0.25, exp=0.63, unclear=0.12)) is Bucket.DISCARD


def test_salary_cap_discards_and_band_reviews():
    decision = laya(junior=0.9, exp=0.05, unclear=0.05)
    assert bucket(make_offer("Dev", desc=LONG, salary_min=45000), decision) is Bucket.DISCARD
    assert bucket(make_offer("Dev", desc=LONG, salary_min=32000), decision) is Bucket.REVIEW
    assert bucket(make_offer("Dev", desc=LONG, salary_min=24000, salary_max=40000), decision) is Bucket.TOP
    assert bucket(make_offer("Dev", desc=LONG), decision) is Bucket.TOP


def test_no_description_never_top():
    o = make_offer("Junior Dev", desc="")
    result = score_offer(o, laya(junior=0.9, exp=0.05, unclear=0.05), CFG, NOW)
    assert result.bucket is Bucket.REVIEW
    assert any("sin descripcion" in n for n in result.notes)


def test_unclear_junior_target_goes_to_review():
    o = make_offer("Dev", desc=LONG)
    assert bucket(o, laya(junior=0.3, exp=0.2, unclear=0.5)) is Bucket.REVIEW


def test_cegid_consultant_low_p_junior_is_not_top_and_prefiltered_by_title():
    cegid = make_offer("Consultor Cegid PeopleNet", desc=LONG)
    assert bucket(cegid, laya(junior=0.05, exp=0.4, unclear=0.55)) is not Bucket.TOP
    assert "not a programming role" in prefilter(cegid, PRE, NOW)


@pytest.mark.parametrize(
    "title",
    [
        "ADMINISTRATIVO/A DE ALMACEN - CARRETILLERO/A",
        "Analista funcional CONTROL M",
        ".NET Specialist | IT Service Owner",
        "Consultora funcional SAP",
        "Consultor Odoo",
        "Técnico/a de sistemas",
        "Técnica de soporte N1",
        "Helpdesk IT",
        "Comercial tecnológico",
    ],
)
def test_non_dev_titles_are_prefiltered(title):
    assert "not a programming role" in (prefilter(make_offer(title), PRE, NOW) or "")


@pytest.mark.parametrize(
    "title",
    ["Junior Python Developer", "Desarrollador/a Frontend React", "Programador Java Junior", "Analista Programador/a"],
)
def test_dev_titles_pass_prefilter(title):
    assert prefilter(make_offer(title), PRE, NOW) is None


def test_non_dev_terms_come_from_search_yaml():
    cfg = load_search_config(__import__("pathlib").Path("config"))
    assert "carretillero" in cfg.prefilter.non_dev_title_terms
    assert isinstance(SearchConfig().prefilter.non_dev_title_terms, list)


def test_dotnet_specialist_with_laya_junior_1_is_still_stopped_by_prefilter():
    o = make_offer(".NET Specialist | IT Service Owner", source="tecnoempleo", desc=LONG)
    assert bucket(o, laya(junior=1.0, exp=0.0, unclear=0.0)) is Bucket.TOP  # Laya alone cannot catch it
    assert prefilter(o, PRE, NOW)  # the title rule does


def test_heuristic_emits_junior_target_consistent_with_seniority():
    import asyncio

    from offerscan.ai.heuristic import HeuristicEngine
    from offerscan.ai.questions import OFFER_QUESTIONS_V1, build_offer_state

    def target(title, desc="", **kw):
        state = build_offer_state(make_offer(title, desc=desc, **kw))
        return asyncio.run(HeuristicEngine().ask(state, OFFER_QUESTIONS_V1)).answers["junior_target"]["choice"]

    assert target("Junior Python Developer") == "junior"
    assert target("Senior Python Developer") == "con_experiencia"
    assert target("Python Developer", "Se requieren 4 anos de experiencia", experience_years_min=4) == "con_experiencia"
    assert target("Python Developer") == "no_claro"
