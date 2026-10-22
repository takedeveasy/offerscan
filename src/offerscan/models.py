from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WorkMode(str, Enum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class Bucket(str, Enum):
    TOP = "top"
    REVIEW = "review"
    DISCARD = "discard"


def make_offer_id(source: str, external_id: str) -> str:
    return hashlib.sha1(f"{source}:{external_id}".encode()).hexdigest()


class JobOffer(BaseModel):
    id: str
    source: str
    external_id: str
    url: str
    title: str
    company: str = ""
    location: str = ""
    work_mode: WorkMode = WorkMode.UNKNOWN
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "EUR"
    contract_type: str = ""
    experience_years_min: int | None = None
    description_md: str = ""
    skills: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    scraped_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)
    dedupe_group: str | None = None

    @property
    def has_salary(self) -> bool:
        return self.salary_min is not None or self.salary_max is not None

    def completeness(self) -> int:
        score = len(self.description_md) + 200 * len(self.skills)
        return score + (500 if self.has_salary else 0)


class OfferDecision(BaseModel):
    offer_id: str
    question_set_version: str
    model: str
    answers: dict[str, Any]
    credits_used: float = 0
    created_at: datetime
    engine: str  # "jev" | "heuristic"


class JuniorProfile(BaseModel):
    """Profile sent (partially) to Jev. Must never contain PII."""

    slug: str
    stack: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    work_mode_pref: list[WorkMode] = Field(default_factory=list)
    min_salary: int | None = None
    experience_months: int = 0
    interests: list[str] = Field(default_factory=list)


class Match(BaseModel):
    offer_id: str
    junior_slug: str
    fit_score: float
    stack_match: float
    final_score: float
    bucket: Bucket
    reasons: list[str] = Field(default_factory=list)
