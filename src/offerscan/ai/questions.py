"""Single source of truth for the typed questions sent to Jev (instructions are in English on purpose)."""

from __future__ import annotations

from typing import Any

from offerscan.models import JobOffer, JuniorProfile

OFFER_VERSION = "offer-v1"
MATCH_VERSION = "match-v1"
LAYA_OFFER_VERSION = "offer-laya-v1"
OFFER_VERSIONS = {OFFER_VERSION, LAYA_OFFER_VERSION}
MAX_DESCRIPTION_CHARS = 6000

EXPERIENCE_LEVELS = ["none", "under_1y", "1_2y", "3_4y", "5y_plus"]
QUALITY_LEVELS = ["poor", "fair", "good", "excellent"]
FIT_LEVELS = ["poor", "partial", "good", "strong"]

OFFER_QUESTIONS_V1: dict[str, dict[str, Any]] = {
    "seniority": {
        "type": "choice",
        "instructions": "What seniority level is this job offer realistically aimed at?",
        "criteria": {
            "internship": "Internship, trainee or apprenticeship position",
            "junior": "Junior or entry-level position, up to about 2 years of experience",
            "mid": "Mid-level position needing roughly 3 to 5 years of experience",
            "senior": "Senior, lead or architect position",
            "unclear": "The seniority cannot be determined from the text",
        },
    },
    "role_family": {
        "type": "choice",
        "instructions": "Which role family best describes the main day-to-day work of this job?",
        "criteria": {
            "backend": "Server-side development, APIs, services",
            "frontend": "Web user interface development",
            "fullstack": "Both frontend and backend development",
            "data_ml": "Data engineering, analytics, machine learning",
            "devops_cloud": "Infrastructure, DevOps, cloud, SRE",
            "qa": "Testing and quality assurance",
            "mobile": "iOS or Android development",
            "other": "Anything else",
        },
    },
    "experience_required": {
        "type": "score",
        "instructions": "How much professional experience does the offer require? Pick the matching level.",
        "criteria": EXPERIENCE_LEVELS,
    },
    "junior_friendly": {
        "type": "noul",
        "instructions": (
            "Would this employer realistically hire someone with less than 1 year of professional experience?"
        ),
    },
    "mentorship": {
        "type": "noul",
        "instructions": "Does the offer mention mentoring, training, onboarding or a career plan?",
    },
    "red_flags": {
        "type": "noul",
        "instructions": (
            "Does the offer show warning signs: unpaid internship, disguised self-employment, "
            "hidden sales work, or absurd requirements for a junior role?"
        ),
    },
    "offer_quality": {
        "type": "score",
        "instructions": (
            "Rate the quality of the offer: clarity, visible salary, concrete tech stack, growth prospects."
        ),
        "criteria": QUALITY_LEVELS,
    },
}

MATCH_QUESTIONS_V1: dict[str, dict[str, Any]] = {
    "junior_fit": {
        "type": "score",
        "instructions": "How well does this offer fit the junior candidate profile (stack, level, location)?",
        "criteria": FIT_LEVELS,
    },
    "stack_match": {
        "type": "noul",
        "instructions": "Does the candidate's main stack cover the essential requirements of the offer?",
    },
}


LAYA_OFFER_QUESTIONS_V1: dict[str, dict[str, Any]] = {
    "seniority": {
        "type": "choice",
        "instructions": "¿Qué nivel de seniority exige realmente esta oferta de empleo?",
        "criteria": {
            "internship": "Prácticas, becarios o trainees",
            "junior": "Junior o nivel inicial, hasta 1-2 años de experiencia",
            "mid": "Nivel medio, de 3 a 5 años de experiencia",
            "senior": "Senior, líder, arquitecto o responsable",
            "unclear": "No se puede saber por el texto",
        },
    },
    "junior_target": {
        "type": "choice",
        "instructions": "¿Para quién es adecuada esta oferta de empleo?",
        "criteria": {
            "junior": "Becarios, prácticas o juniors con 0-1 años de experiencia",
            "con_experiencia": "Candidatos con 2 o más años de experiencia",
            "no_claro": "No se puede saber por el texto",
        },
    },
    "experience_required": OFFER_QUESTIONS_V1["experience_required"],
}


def build_offer_state(offer: JobOffer) -> dict[str, Any]:
    return {
        "title": offer.title,
        "company": offer.company,
        "location": offer.location,
        "work_mode": offer.work_mode.value,
        "salary": (
            f"{offer.salary_min}-{offer.salary_max} EUR/year" if offer.has_salary else "not stated"
        ),
        "experience_years_min": offer.experience_years_min,
        "skills": offer.skills,
        "description": offer.description_md[:MAX_DESCRIPTION_CHARS],
    }


def build_match_state(offer: JobOffer, junior: JuniorProfile) -> dict[str, Any]:
    """Compact offer + profile. Profiles carry no PII by construction."""
    return {
        "offer": {
            "title": offer.title,
            "location": offer.location,
            "work_mode": offer.work_mode.value,
            "skills": offer.skills,
            "experience_years_min": offer.experience_years_min,
            "summary": offer.description_md[:1500],
        },
        "candidate": {
            "stack": junior.stack,
            "nice_to_have": junior.nice_to_have,
            "languages": junior.languages,
            "locations": junior.locations,
            "work_mode_pref": [m.value for m in junior.work_mode_pref],
            "experience_months": junior.experience_months,
            "interests": junior.interests,
        },
    }
