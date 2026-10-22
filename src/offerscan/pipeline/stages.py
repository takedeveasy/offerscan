from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from offerscan.pipeline.dedupe import dedupe
from offerscan.pipeline.normalize import normalize_raw
from offerscan.pipeline.prefilter import prefilter
from offerscan.settings import SearchConfig
from offerscan.storage.db import (
    STATUS_CANDIDATE,
    STATUS_DUPLICATE,
    STATUS_EVALUATED,
    STATUS_NEW,
    STATUS_PREFILTERED,
    Database,
)


@dataclass
class PrepareStats:
    new: int = 0
    duplicates: int = 0
    prefiltered: int = 0
    candidates: int = 0


def ingest_raw(db: Database, raws: list[dict], cfg: SearchConfig, now: datetime | None = None) -> int:
    """normalize -> persist. Returns number of offers stored (new or refreshed)."""
    offers = [normalize_raw(r, now, cfg.salary.monthly_payments) for r in raws]
    added, updated = db.upsert_offers(offers)
    return added + updated


def prepare(db: Database, cfg: SearchConfig, now: datetime | None = None) -> PrepareStats:
    """dedupe -> prefilter every offer in status `new`; survivors become `candidate`."""
    stats = PrepareStats()
    fresh = db.offers_by_status(STATUS_NEW)
    stats.new = len(fresh)
    if not fresh:
        return stats
    existing = db.offers_by_status(STATUS_CANDIDATE, STATUS_EVALUATED)
    fresh_ids = {o.id for o in fresh}
    kept, dupes = dedupe(existing + fresh)
    for offer in dupes:
        if offer.id in fresh_ids:
            db.set_status(offer.id, STATUS_DUPLICATE, "duplicate offer", offer.dedupe_group)
            stats.duplicates += 1
    for offer in kept:
        if offer.id not in fresh_ids:
            continue
        reason = prefilter(offer, cfg.prefilter, now)
        if reason:
            db.set_status(offer.id, STATUS_PREFILTERED, reason, offer.dedupe_group)
            stats.prefiltered += 1
        else:
            db.set_status(offer.id, STATUS_CANDIDATE, None, offer.dedupe_group)
            stats.candidates += 1
    return stats
