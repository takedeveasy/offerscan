from __future__ import annotations

import hashlib
import re

from rapidfuzz import fuzz

from offerscan.models import JobOffer
from offerscan.pipeline.normalize import strip_accents

_GENDER_RE = re.compile(r"\((?:[mhf]\s*/\s*[mhfd]|[mhf]\s*/\s*[mhf]\s*/\s*[dx])\)|\b[mhf]/[mhfd]\b", re.I)
_LEGAL_RE = re.compile(r"\b(s\.?l\.?u?|s\.?a\.?u?|s\.?l\.?l|ltd|inc|gmbh|sociedad limitada)\b\.?", re.I)

TITLE_THRESHOLD = 90
COMPANY_THRESHOLD = 90


def normalize_key(text: str) -> str:
    text = strip_accents(text).lower()
    text = _GENDER_RE.sub(" ", text)
    text = _LEGAL_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def are_duplicates(a: JobOffer, b: JobOffer) -> bool:
    if a.source == b.source and a.external_id == b.external_id:
        return True
    company_a, company_b = normalize_key(a.company), normalize_key(b.company)
    if not company_a or not company_b:
        return False
    if fuzz.token_set_ratio(company_a, company_b) < COMPANY_THRESHOLD:
        return False
    # token_sort (not token_set): "python dev" must not match "senior python dev"
    return fuzz.token_sort_ratio(normalize_key(a.title), normalize_key(b.title)) >= TITLE_THRESHOLD


def _group_id(offer: JobOffer) -> str:
    key = f"{normalize_key(offer.company)}|{normalize_key(offer.title)}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def dedupe(offers: list[JobOffer]) -> tuple[list[JobOffer], list[JobOffer]]:
    """Return (kept, duplicates). Keeps the most complete offer of every group.

    Offers get `dedupe_group` assigned. Complexity is O(n * groups); fine for a few thousand offers.
    """
    groups: list[list[JobOffer]] = []
    for offer in offers:
        for group in groups:
            if any(are_duplicates(offer, member) for member in group):
                group.append(offer)
                break
        else:
            groups.append([offer])
    kept: list[JobOffer] = []
    dupes: list[JobOffer] = []
    for group in groups:
        best = max(group, key=JobOffer.completeness)
        gid = _group_id(best)
        for offer in group:
            offer.dedupe_group = gid
            (kept if offer is best else dupes).append(offer)
    return kept, dupes
