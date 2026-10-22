from datetime import datetime, timedelta, timezone

import pytest

from offerscan.models import JobOffer, OfferDecision, WorkMode, make_offer_id
from offerscan.pipeline.dedupe import dedupe
from offerscan.pipeline.prefilter import prefilter
from offerscan.pipeline.stages import prepare
from offerscan.settings import PrefilterConfig, SearchConfig
from offerscan.storage.db import STATUS_CANDIDATE, STATUS_DUPLICATE, STATUS_PREFILTERED, Database

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)


def make(source="a", ext="1", title="Junior Python Developer", company="Acme S.L.", **kw) -> JobOffer:
    return JobOffer(
        id=make_offer_id(source, ext),
        source=source,
        external_id=ext,
        url=f"https://x/{source}/{ext}",
        title=title,
        company=company,
        scraped_at=NOW,
        **kw,
    )


def test_dedupe_cross_portal_keeps_most_complete():
    short = make("a", "1")
    full = make("b", "9", title="Junior Python Developer (M/F)", company="ACME", description_md="x" * 500)
    other = make("a", "2", title="Senior Python Developer")
    kept, dupes = dedupe([short, full, other])
    assert full in kept and short in dupes and other in kept
    assert full.dedupe_group == short.dedupe_group != other.dedupe_group


def test_dedupe_same_external_id():
    a, b = make("a", "1"), make("a", "1", company="")
    kept, dupes = dedupe([a, b])
    assert len(kept) == 1 and len(dupes) == 1


def test_dedupe_different_company_not_merged():
    kept, dupes = dedupe([make(company="Acme"), make("b", "2", company="Globex Corporation")])
    assert len(kept) == 2 and not dupes


CFG = PrefilterConfig(company_blacklist=["Bad Corp"])

SPANISH_SENIOR = [
    "Arquitecto/a Software Java", "Arquitecta de datos", "Jefe de Proyecto", "Jefa/e de equipo IT",
    "Responsable de Sistemas", "Director de Tecnología", "Directora de TI", "Líder técnico Python",
    "LIDER Tecnico", "Experto/a en Cloud", "Tech Lead Backend", "Team Lead Frontend", "Staff Engineer",
    "SR Developer", "Sr. Java Dev", "Desarrollador Senior",
]  # fmt: skip


@pytest.mark.parametrize("title", SPANISH_SENIOR)
def test_prefilter_spanish_titles(title):
    assert prefilter(make(title=title), PrefilterConfig(), NOW) is not None


@pytest.mark.parametrize(
    "title", ["Desarrollador Python Junior", "Programador/a Backend", "Analista Programador", "Desarrollador Fullstack"]
)
def test_prefilter_spanish_does_not_overreach(title):
    assert prefilter(make(title=title), PrefilterConfig(), NOW) is None



@pytest.mark.parametrize(
    "kwargs,expect_reason",
    [
        ({"title": "Senior Python Developer"}, "senior"),
        ({"title": "Sr. Java Engineer"}, "sr"),
        ({"title": "Tech Lead Backend"}, "lead"),
        ({"title": "Head of Engineering"}, "head of"),
        ({"title": "Junior Developer", "experience_years_min": 3}, "3+ years"),
        ({"title": "Junior Developer", "experience_years_min": 2}, None),
        ({"title": "Junior Developer", "published_at": NOW - timedelta(days=45)}, "45 days"),
        ({"title": "Junior Developer", "published_at": NOW - timedelta(days=5)}, None),
        ({"title": "Junior Developer", "company": "Bad Corp S.L."}, "blacklisted"),
        ({"title": "Seniority-free Leader Junior"}, None),
    ],
)
def test_prefilter(kwargs, expect_reason):
    offer = make(**kwargs)
    reason = prefilter(offer, CFG, NOW)
    if expect_reason is None:
        assert reason is None
    else:
        assert expect_reason in reason


def test_prepare_and_statuses():
    db = Database(":memory:")
    offers = [
        make("a", "1"),
        make("b", "2", company="acme"),
        make("a", "3", title="Senior Dev", company="Other"),
    ]
    db.upsert_offers(offers)
    stats = prepare(db, SearchConfig(), NOW)
    assert (stats.duplicates, stats.prefiltered, stats.candidates) == (1, 1, 1)
    counts = db.status_counts()
    assert counts[STATUS_DUPLICATE] == counts[STATUS_PREFILTERED] == counts[STATUS_CANDIDATE] == 1
    assert "senior" in next(r for r in db.discard_reasons().values() if "senior" in r)
    # idempotent: nothing new to process
    assert prepare(db, SearchConfig(), NOW).new == 0


def test_upsert_keeps_status_and_decision_roundtrip():
    db = Database(":memory:")
    o = make()
    assert db.upsert_offers([o]) == (1, 0)
    db.set_status(o.id, STATUS_CANDIDATE)
    assert db.upsert_offers([o]) == (0, 1)
    assert [x.id for x in db.offers_by_status(STATUS_CANDIDATE)] == [o.id]
    d = OfferDecision(
        offer_id=o.id, question_set_version="v1", model="m", answers={"a": 1}, created_at=NOW, engine="jev"
    )
    db.save_decision(d)
    db.save_decision(d.model_copy(update={"answers": {"a": 2}}))
    assert db.get_decision(o.id, "v1", "m").answers == {"a": 2}
    assert db.get_decision(o.id, "v2", "m") is None
    assert len(db.all_decisions()) == 1
    assert o.work_mode is WorkMode.UNKNOWN
