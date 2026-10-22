from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select

from offerscan.models import JobOffer, Match, OfferDecision

# Offer lifecycle: new -> duplicate | prefiltered | candidate -> evaluated
STATUS_NEW = "new"
STATUS_DUPLICATE = "duplicate"
STATUS_PREFILTERED = "prefiltered"
STATUS_CANDIDATE = "candidate"
STATUS_EVALUATED = "evaluated"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OfferRow(SQLModel, table=True):
    __tablename__ = "offers"
    id: str = Field(primary_key=True)
    source: str = Field(index=True)
    status: str = Field(default=STATUS_NEW, index=True)
    discard_reason: str | None = None
    dedupe_group: str | None = Field(default=None, index=True)
    data: str  # JobOffer JSON


class DecisionRow(SQLModel, table=True):
    __tablename__ = "decisions"
    __table_args__ = (UniqueConstraint("offer_id", "question_set_version", "model"),)
    id: int | None = Field(default=None, primary_key=True)
    offer_id: str = Field(index=True)
    question_set_version: str
    model: str
    engine: str
    data: str  # OfferDecision JSON


class MatchRow(SQLModel, table=True):
    __tablename__ = "matches"
    offer_id: str = Field(primary_key=True)
    junior_slug: str = Field(primary_key=True)
    data: str  # Match JSON


class RunRow(SQLModel, table=True):
    __tablename__ = "runs"
    id: int | None = Field(default=None, primary_key=True)
    source: str
    started_at: datetime = Field(default_factory=_now)
    fetched: int = 0
    parsed: int = 0
    failed: int = 0
    blocked: int = 0
    notes: str = ""


LABEL_JUNIOR = "junior"
LABEL_NOT_JUNIOR = "no_junior"


class LabelRow(SQLModel, table=True):
    """Human verdict on an offer ("is it really a junior job?"); feeds `offerscan eval`."""

    __tablename__ = "labels"
    offer_id: str = Field(primary_key=True)
    label: str
    labeled_at: datetime = Field(default_factory=_now)


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{path}"
        self.engine = create_engine(url, connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

    # offers
    def upsert_offers(self, offers: Iterable[JobOffer]) -> tuple[int, int]:
        """Insert unseen offers; refresh data of known ones keeping their status."""
        added = updated = 0
        with Session(self.engine) as s:
            for offer in offers:
                row = s.get(OfferRow, offer.id)
                payload = offer.model_dump_json()
                if row is None:
                    s.add(OfferRow(id=offer.id, source=offer.source, data=payload))
                    added += 1
                else:
                    row.data = payload
                    updated += 1
            s.commit()
        return added, updated

    def offers_by_status(self, *statuses: str) -> list[JobOffer]:
        with Session(self.engine) as s:
            rows = s.exec(select(OfferRow).where(OfferRow.status.in_(statuses))).all()
        return [JobOffer.model_validate_json(r.data) for r in rows]

    def all_offers(self) -> list[JobOffer]:
        with Session(self.engine) as s:
            return [JobOffer.model_validate_json(r.data) for r in s.exec(select(OfferRow)).all()]

    def get_offer(self, offer_id: str) -> JobOffer | None:
        with Session(self.engine) as s:
            row = s.get(OfferRow, offer_id)
        return JobOffer.model_validate_json(row.data) if row else None

    def set_status(
        self,
        offer_id: str,
        status: str,
        reason: str | None = None,
        dedupe_group: str | None = None,
    ) -> None:
        with Session(self.engine) as s:
            row = s.get(OfferRow, offer_id)
            if row is None:
                return
            row.status = status
            row.discard_reason = reason
            if dedupe_group is not None:
                row.dedupe_group = dedupe_group
                offer = JobOffer.model_validate_json(row.data)
                offer.dedupe_group = dedupe_group
                row.data = offer.model_dump_json()
            s.add(row)
            s.commit()

    def discard_reasons(self) -> dict[str, str]:
        with Session(self.engine) as s:
            rows = s.exec(select(OfferRow).where(OfferRow.discard_reason.is_not(None))).all()
        return {r.id: r.discard_reason or "" for r in rows}

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with Session(self.engine) as s:
            for row in s.exec(select(OfferRow)).all():
                counts[row.status] = counts.get(row.status, 0) + 1
        return counts

    # decisions
    def get_decision(self, offer_id: str, version: str, model: str) -> OfferDecision | None:
        with Session(self.engine) as s:
            row = s.exec(
                select(DecisionRow).where(
                    DecisionRow.offer_id == offer_id,
                    DecisionRow.question_set_version == version,
                    DecisionRow.model == model,
                )
            ).first()
        return OfferDecision.model_validate_json(row.data) if row else None

    def save_decision(self, decision: OfferDecision) -> None:
        with Session(self.engine) as s:
            row = s.exec(
                select(DecisionRow).where(
                    DecisionRow.offer_id == decision.offer_id,
                    DecisionRow.question_set_version == decision.question_set_version,
                    DecisionRow.model == decision.model,
                )
            ).first()
            if row is None:
                row = DecisionRow(
                    offer_id=decision.offer_id,
                    question_set_version=decision.question_set_version,
                    model=decision.model,
                    engine=decision.engine,
                    data="",
                )
            row.engine = decision.engine
            row.data = decision.model_dump_json()
            s.add(row)
            s.commit()

    def decisions_for(self, offer_id: str) -> list[OfferDecision]:
        with Session(self.engine) as s:
            rows = s.exec(select(DecisionRow).where(DecisionRow.offer_id == offer_id)).all()
        return [OfferDecision.model_validate_json(r.data) for r in rows]

    def all_decisions(self) -> list[OfferDecision]:
        with Session(self.engine) as s:
            rows = s.exec(select(DecisionRow)).all()
        return [OfferDecision.model_validate_json(r.data) for r in rows]

    def all_matches(self) -> list[Match]:
        with Session(self.engine) as s:
            return [Match.model_validate_json(r.data) for r in s.exec(select(MatchRow)).all()]

    def offer_rows(self) -> list[OfferRow]:
        with Session(self.engine) as s:
            return list(s.exec(select(OfferRow)).all())

    # labels
    def set_label(self, offer_id: str, label: str | None) -> None:
        """`None` removes the label."""
        with Session(self.engine) as s:
            row = s.get(LabelRow, offer_id)
            if label is None:
                if row is not None:
                    s.delete(row)
            else:
                if label not in (LABEL_JUNIOR, LABEL_NOT_JUNIOR):
                    raise ValueError(f"etiqueta desconocida: {label}")
                if row is None:
                    row = LabelRow(offer_id=offer_id, label=label)
                row.label = label
                row.labeled_at = _now()
                s.add(row)
            s.commit()

    def labels(self) -> dict[str, str]:
        with Session(self.engine) as s:
            return {r.offer_id: r.label for r in s.exec(select(LabelRow)).all()}

    # matches
    def save_match(self, match: Match) -> None:
        with Session(self.engine) as s:
            row = s.get(MatchRow, (match.offer_id, match.junior_slug))
            if row is None:
                row = MatchRow(offer_id=match.offer_id, junior_slug=match.junior_slug, data="")
            row.data = match.model_dump_json()
            s.add(row)
            s.commit()

    def matches_for(self, junior_slug: str) -> list[Match]:
        with Session(self.engine) as s:
            rows = s.exec(select(MatchRow).where(MatchRow.junior_slug == junior_slug)).all()
        return [Match.model_validate_json(r.data) for r in rows]

    def clear_matches(self, junior_slug: str) -> None:
        with Session(self.engine) as s:
            for row in s.exec(select(MatchRow).where(MatchRow.junior_slug == junior_slug)).all():
                s.delete(row)
            s.commit()

    # runs
    def add_run(self, source: str, **counters: int | str) -> None:
        with Session(self.engine) as s:
            s.add(RunRow(source=source, **counters))
            s.commit()

    def runs(self) -> list[RunRow]:
        with Session(self.engine) as s:
            return list(s.exec(select(RunRow)).all())


def dump_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
