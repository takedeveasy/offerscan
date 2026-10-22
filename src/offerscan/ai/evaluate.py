from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from offerscan.ai.jev_client import (
    JevAuthError,
    JevBudgetExceeded,
    JevCreditsError,
    JevError,
    JevResult,
    JevUnreachable,
)
from offerscan.ai.questions import (
    MATCH_QUESTIONS_V1,
    MATCH_VERSION,
    OFFER_QUESTIONS_V1,
    OFFER_VERSION,
    build_match_state,
    build_offer_state,
)
from offerscan.models import JobOffer, JuniorProfile, OfferDecision
from offerscan.storage.db import STATUS_EVALUATED, Database

log = logging.getLogger("offerscan.evaluate")


class Engine(Protocol):
    model: str
    engine_name: str

    async def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> JevResult: ...

    async def aclose(self) -> None: ...


@dataclass
class EvalStats:
    evaluated: int = 0
    cached: int = 0
    failed: int = 0
    credits: float = 0.0
    stopped_reason: str = ""
    errors: list[str] = field(default_factory=list)


def match_decision_id(offer_id: str, junior_slug: str) -> str:
    return f"{offer_id}:{junior_slug}"


async def _decide(
    db: Database,
    engine: Engine,
    decision_id: str,
    state: Any,
    questions: dict[str, dict[str, Any]],
    version: str,
    stats: EvalStats,
    stop: asyncio.Event,
) -> OfferDecision | None:
    cached = db.get_decision(decision_id, version, engine.model)
    if cached:
        stats.cached += 1
        return cached
    if stop.is_set():
        return None
    try:
        result = await engine.ask(state, questions)
    except (JevAuthError, JevCreditsError, JevBudgetExceeded, JevUnreachable) as exc:
        stats.stopped_reason = str(exc)
        stop.set()
        return None
    except JevError as exc:
        stats.failed += 1
        stats.errors.append(f"{decision_id}: {exc}")
        return None
    return _store(db, engine, decision_id, result, version, stats)


def _store(
    db: Database, engine: Engine, decision_id: str, result: JevResult, version: str, stats: EvalStats
) -> OfferDecision:
    decision = OfferDecision(
        offer_id=decision_id,
        question_set_version=version,
        model=engine.model,
        answers=result.answers,
        credits_used=result.credits_used,
        created_at=datetime.now(timezone.utc),
        engine=engine.engine_name,
    )
    db.save_decision(decision)
    stats.evaluated += 1
    stats.credits += result.credits_used
    return decision


async def evaluate_offers(
    db: Database,
    engine: Engine,
    offers: list[JobOffer],
    *,
    on_progress: Callable[[int], None] | None = None,
) -> EvalStats:
    """One request per offer (or per batch for engines with `ask_batch`) with all OFFER questions.

    Offers whose request failed stay pending.
    """
    stats = EvalStats()
    stop = asyncio.Event()
    done = 0
    offer_version = getattr(engine, "offer_version", OFFER_VERSION)
    # Only engines that declare a `batch_size` (Laya) are batched; the Jev API has no batch route.
    ask_batch = getattr(engine, "ask_batch", None) if getattr(engine, "batch_size", 0) > 1 else None

    async def one(offer: JobOffer) -> None:
        nonlocal done
        decision = await _decide(
            db, engine, offer.id, build_offer_state(offer), OFFER_QUESTIONS_V1, offer_version, stats, stop
        )
        if decision is not None:
            db.set_status(offer.id, STATUS_EVALUATED)
        done += 1
        if on_progress:
            on_progress(done)

    if ask_batch is not None:
        pending = []
        for offer in offers:
            if db.get_decision(offer.id, offer_version, engine.model):
                stats.cached += 1
                db.set_status(offer.id, STATUS_EVALUATED)
                done += 1
                if on_progress:
                    on_progress(done)
            else:
                pending.append(offer)
        size = max(int(getattr(engine, "batch_size", 1)), 1)
        for start in range(0, len(pending), size):
            if stop.is_set():
                break
            chunk = pending[start : start + size]
            try:
                results = await ask_batch([build_offer_state(o) for o in chunk], OFFER_QUESTIONS_V1)
            except (JevAuthError, JevCreditsError, JevBudgetExceeded, JevUnreachable) as exc:
                stats.stopped_reason = str(exc)
                stop.set()
                break
            except JevError as exc:  # a bad batch should not sink its neighbours: retry one by one
                log.warning("batch of %d failed (%s); falling back to single requests", len(chunk), exc)
                results = None
            if results is None:
                await asyncio.gather(*(one(o) for o in chunk))
                continue
            for offer, result in zip(chunk, results, strict=True):
                _store(db, engine, offer.id, result, offer_version, stats)
                db.set_status(offer.id, STATUS_EVALUATED)
                done += 1
                if on_progress:
                    on_progress(done)
        stats.credits = round(stats.credits, 4)
        return stats

    await asyncio.gather(*(one(o) for o in offers))
    stats.credits = round(stats.credits, 4)
    return stats


async def match_offers(
    db: Database,
    engine: Engine,
    junior: JuniorProfile,
    offers: list[JobOffer],
) -> tuple[dict[str, OfferDecision], EvalStats]:
    """Ask MATCH questions for (offer, junior) pairs; returns decisions keyed by offer id."""
    stats = EvalStats()
    stop = asyncio.Event()
    out: dict[str, OfferDecision] = {}

    async def one(offer: JobOffer) -> None:
        decision = await _decide(
            db,
            engine,
            match_decision_id(offer.id, junior.slug),
            build_match_state(offer, junior),
            MATCH_QUESTIONS_V1,
            MATCH_VERSION,
            stats,
            stop,
        )
        if decision is not None:
            out[offer.id] = decision

    await asyncio.gather(*(one(o) for o in offers))
    return out, stats
