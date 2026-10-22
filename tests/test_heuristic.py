import asyncio
from datetime import datetime, timezone

from offerscan.ai.heuristic import HeuristicEngine
from offerscan.ai.jev_client import normalize_answer
from offerscan.ai.questions import (
    MATCH_QUESTIONS_V1,
    OFFER_QUESTIONS_V1,
    build_match_state,
    build_offer_state,
)
from offerscan.models import JobOffer, JuniorProfile, WorkMode, make_offer_id

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def offer(title, desc, **kw) -> JobOffer:
    return JobOffer(
        id=make_offer_id("t", title), source="t", external_id=title, url="u", title=title,
        company="c", scraped_at=NOW, description_md=desc, **kw,
    )  # fmt: skip


def ask(state, questions):
    return asyncio.run(HeuristicEngine().ask(state, questions)).answers


def test_junior_offer_scores_friendly():
    o = offer("Junior Backend Developer", "Python, plan de formacion y mentoring. Sin experiencia previa.",
              experience_years_min=0, skills=["python"])  # fmt: skip
    a = ask(build_offer_state(o), OFFER_QUESTIONS_V1)
    assert a["seniority"]["choice"] == "junior"
    assert a["junior_friendly"]["noul"] > 0.8
    assert a["mentorship"]["noul"] > 0.3
    assert a["red_flags"]["noul"] < 0.2
    assert a["role_family"]["choice"] == "backend"
    assert a["experience_required"]["score"] == "none"


def test_senior_offer_scores_unfriendly_and_red_flags():
    o = offer("Senior Java Architect", "Mas de 7 anos de experiencia. Ventas, comision y autonomo.",
              experience_years_min=7)  # fmt: skip
    a = ask(build_offer_state(o), OFFER_QUESTIONS_V1)
    assert a["seniority"]["choice"] == "senior"
    assert a["junior_friendly"]["noul"] < 0.1
    assert a["red_flags"]["noul"] >= 0.5
    assert a["experience_required"]["score"] == "5y_plus"


def test_shapes_match_jev_normalization():
    o = offer("Dev", "texto")
    answers = ask(build_offer_state(o), OFFER_QUESTIONS_V1)
    for qid, ans in answers.items():
        if qid not in OFFER_QUESTIONS_V1:  # extra `junior_target` signal
            continue
        again = normalize_answer(OFFER_QUESTIONS_V1[qid]["type"], ans, OFFER_QUESTIONS_V1[qid])
        assert again["type"] == ans["type"]


def test_match_fit():
    junior = JuniorProfile(slug="a", stack=["python", "django", "docker"], locations=["Madrid"],
                           work_mode_pref=[WorkMode.HYBRID], experience_months=6)  # fmt: skip
    good = offer("Junior Python Django", "Docker y APIs", location="Madrid", work_mode=WorkMode.HYBRID,
                 skills=["python", "django", "docker"])  # fmt: skip
    bad = offer("Junior Cobol", "mainframe", location="Sevilla", work_mode=WorkMode.ONSITE)
    ga = ask(build_match_state(good, junior), MATCH_QUESTIONS_V1)
    ba = ask(build_match_state(bad, junior), MATCH_QUESTIONS_V1)
    assert ga["stack_match"]["noul"] > 0.8 > ba["stack_match"]["noul"]
    assert ga["junior_fit"]["index"] > ba["junior_fit"]["index"]


def seniority(title, desc="", years=None):
    o = offer(title, desc, experience_years_min=years)
    return ask(build_offer_state(o), {"seniority": OFFER_QUESTIONS_V1["seniority"]})["seniority"]["choice"]


def test_seniority_needs_positive_junior_evidence():
    assert seniority("Arquitecto/a Software Java") == "senior"
    assert seniority("Jefe de proyecto") == "senior"
    assert seniority("Programadores Java") == "unclear"
    assert seniority("Programadores Java", years=2) == "mid"
    assert seniority("Programadores Java", years=1) == "junior"
    assert seniority("Programador Java", "buscamos perfil junior, sin experiencia") == "junior"
    assert seniority("Becario desarrollo") == "internship"


def test_juniors_in_the_team_are_not_junior_evidence():
    assert seniority("Desarrollador Java", "Mentorizaras a perfiles junior del equipo.") == "unclear"
    assert seniority("Desarrollador Java", "Tendras a cargo de 2 juniors.") == "unclear"
    assert seniority("Desarrollador Java", "Buscamos perfiles junior con ganas.") == "junior"


def red_flags(title, desc):
    return ask(build_offer_state(offer(title, desc)), {"red_flags": OFFER_QUESTIONS_V1["red_flags"]})["red_flags"]["noul"]


def test_sales_words_only_flag_the_title():
    assert red_flags("Backend Developer", "Plataforma de ventas online, equipo comercial.") < 0.2
    assert red_flags("Comercial junior", "Captacion de clientes y comisiones.") >= 0.75
    assert red_flags("Backend Developer", "Trabajo con autonomia.") < 0.2
