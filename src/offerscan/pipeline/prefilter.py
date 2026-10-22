from __future__ import annotations

import re
from datetime import datetime, timezone

from offerscan.models import JobOffer
from offerscan.pipeline.dedupe import normalize_key
from offerscan.pipeline.normalize import strip_accents
from offerscan.settings import PrefilterConfig


def _word_pattern(word: str) -> str:
    if word.isalpha() and len(word) >= 4:
        stem = word[:-1] if word[-1] in "oae" else word
        return re.escape(stem) + r"(?:o|a|e|as|os|es|s)?"
    return re.escape(word)


def _title_regex(terms: list[str]) -> re.Pattern[str] | None:
    """Accent-insensitive; alphabetic words also match gender/plural endings (arquitecto/a, directoras)."""
    parts = []
    for term in terms:
        flat = strip_accents(term).lower().strip()
        if flat:
            parts.append(r"\s+".join(_word_pattern(w) for w in flat.split()))
    if not parts:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


def _non_dev_match(title: str, terms: list[str]) -> str | None:
    """First term matching the title. `a & b|c` needs `a` and any of `b`, `c`."""
    flat = re.sub(r"/(?:a|as|o|os)\b", "", strip_accents(title))  # tecnico/a -> tecnico
    for term in terms:
        clauses = [_title_regex([alt for alt in clause.split("|")]) for clause in term.split("&")]
        found = [c.search(flat) for c in clauses if c is not None]
        if found and all(found):
            return found[0].group(0).lower()
    return None


def prefilter(offer: JobOffer, cfg: PrefilterConfig, now: datetime | None = None) -> str | None:
    """Return a human-readable discard reason, or None if the offer passes."""
    now = now or datetime.now(timezone.utc)
    pattern = _title_regex(cfg.title_blacklist)
    if pattern and (m := pattern.search(strip_accents(offer.title))):
        return f"title contains '{m.group(0).lower()}'"
    if m := _non_dev_match(offer.title, cfg.non_dev_title_terms):
        return f"title is not a programming role ('{m}')"
    years = offer.experience_years_min
    if years is not None and years > cfg.max_experience_years:
        return f"requires {years}+ years of experience"
    if offer.published_at is not None:
        published = offer.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age = (now - published).days
        if age > cfg.max_age_days:
            return f"published {age} days ago"
    company = normalize_key(offer.company)
    if company and any(normalize_key(c) == company for c in cfg.company_blacklist):
        return f"company '{offer.company}' is blacklisted"
    return None
