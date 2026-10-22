"""Pipeline stages shared by the CLI commands (evaluate, match, report)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from offerscan.ai.evaluate import Engine, EvalStats, evaluate_offers
from offerscan.ai.heuristic import HeuristicEngine
from offerscan.ai.jev_client import JevClient
from offerscan.ai.questions import OFFER_VERSIONS
from offerscan.geo import matches_location
from offerscan.models import Bucket, JobOffer, JuniorProfile, Match, OfferDecision
from offerscan.pipeline.normalize import strip_accents
from offerscan.pipeline.stages import PrepareStats, prepare
from offerscan.ranking.matcher import match_junior
from offerscan.ranking.offer_score import latest_offer_decision, rank_offers, score_offer
from offerscan.report.render import FORMATS, build_rows, write_reports
from offerscan.scrapers.runner import SourceResult
from offerscan.scrapers.runner import crawl as run_crawl
from offerscan.settings import ScoringConfig, SearchConfig, Settings, SourceConfig
from offerscan.storage.db import (
    STATUS_CANDIDATE,
    STATUS_DUPLICATE,
    STATUS_EVALUATED,
    STATUS_PREFILTERED,
    Database,
)


def resolve_engine(requested: str | None, settings: Settings) -> tuple[str, str]:
    """Return (engine name, user-facing note). `jev` without a key falls back to heuristic."""
    if requested in ("heuristic", "laya"):
        return requested, ""
    if settings.has_jev_key:
        return "jev", ""
    if requested == "jev":
        return "heuristic", "JEV_API_KEY no esta configurada: se usa el motor heuristico."
    return "heuristic", "Sin JEV_API_KEY: se usa el motor heuristico (usa --engine jev con clave para Jev)."


def make_engine(name: str, settings: Settings) -> Engine:
    if name == "laya":
        from offerscan.ai.laya import make_laya_engine

        return make_laya_engine(settings)
    if name == "jev" and settings.jev_api_key:
        return JevClient(
            settings.jev_api_key.get_secret_value(),
            settings.jev_api_base_url,
            settings.jev_model,
            max_credits=settings.jev_max_credits_per_run,
            concurrency=settings.jev_concurrency,
        )
    return HeuristicEngine()


async def _preflight(engine: Engine) -> None:
    check = getattr(engine, "preflight", None)
    if check:
        await check()


def evaluate_stage(
    db: Database,
    settings: Settings,
    search_cfg: SearchConfig,
    engine_name: str,
    limit: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> tuple[PrepareStats, EvalStats]:
    prep = prepare(db, search_cfg)
    offers = db.offers_by_status(STATUS_CANDIDATE, STATUS_EVALUATED)
    if limit:
        offers = offers[:limit]

    async def go() -> EvalStats:
        engine = make_engine(engine_name, settings)
        try:
            await _preflight(engine)
            return await evaluate_offers(db, engine, offers, on_progress=on_progress)
        finally:
            await engine.aclose()

    return prep, asyncio.run(go())


def match_stage(
    db: Database,
    settings: Settings,
    scoring: ScoringConfig,
    engine_name: str,
    juniors: list[JuniorProfile],
) -> dict[str, tuple[list[Match], EvalStats]]:
    ranked = rank_offers(db, db.offers_by_status(STATUS_EVALUATED), scoring)

    async def go() -> dict[str, tuple[list[Match], EvalStats]]:
        engine = make_engine(engine_name, settings)
        try:
            return {j.slug: await match_junior(db, engine, j, ranked, scoring) for j in juniors}
        finally:
            await engine.aclose()

    return asyncio.run(go())


def report_stage(
    db: Database,
    scoring: ScoringConfig,
    juniors: list[JuniorProfile],
    out_dir: Path,
    formats: list[str],
    engine_name: str | None = None,
) -> list[Path]:
    paths: list[Path] = []
    for junior in juniors:
        matches = db.matches_for(junior.slug)
        rows = build_rows(db, matches, scoring)
        label = engine_name or _decision_engines(db, matches)
        paths += write_reports(out_dir, junior, rows, formats or list(FORMATS), label)
    return paths


def _decision_engines(db: Database, matches: list[Match]) -> str:
    engines = {
        d.engine for m in matches if (d := latest_offer_decision(db, m.offer_id)) is not None
    }
    return ", ".join(sorted(engines)) or "-"


# ---------------------------------------------------------------------------------------------
# Search (crawl -> evaluate -> match) driven by in-memory filters; used by the web UI.
# ---------------------------------------------------------------------------------------------

TECH_SUGGESTIONS = [
    "python", "java", "javascript", "typescript", "react", "angular", "vue", "node", "php", ".net", "c#",
    "go", "kotlin", "swift", "sql", "data", "devops", "qa",
]  # fmt: skip


def laya_available(base_url: str) -> bool:
    import httpx

    try:
        return httpx.get(f"{base_url.rstrip('/')}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


class SearchFilters(BaseModel):
    keywords: list[str] = Field(default_factory=lambda: ["python"])
    locations: list[str] = Field(default_factory=list)  # province names and/or "Remoto"
    sources: list[str] = Field(default_factory=lambda: ["tecnoempleo"])
    work_modes: list[str] = Field(default_factory=list)  # onsite | hybrid | remote; empty = any
    max_experience_years: int = 2
    max_age_days: int = 30
    salary_min: int | None = None
    junior_salary_max: int = 35_000
    engine: str = "heuristic"
    max_pages: int = 1
    match_juniors: bool = True


def apply_search_filters(base: SearchConfig, filters: SearchFilters) -> SearchConfig:
    """In-memory SearchConfig: the UI never rewrites config/search.yaml."""
    cfg = base.model_copy(deep=True)
    cfg.keywords = filters.keywords or cfg.keywords
    cfg.locations = filters.locations
    cfg.work_modes = filters.work_modes
    cfg.prefilter.max_experience_years = filters.max_experience_years
    cfg.prefilter.max_age_days = filters.max_age_days
    for name in {*cfg.sources, *filters.sources}:
        source = cfg.sources.setdefault(name, SourceConfig())
        source.enabled = name in filters.sources
        source.max_pages = filters.max_pages
        # Without a description an offer can never be "top": give every keyword/page enough detail fetches.
        source.max_details = min(max(source.max_details, 20 * len(cfg.keywords) * filters.max_pages), 100)
    return cfg


@dataclass
class SearchHooks:
    """Optional callbacks so a caller (the UI thread) can show live progress."""

    stage: Callable[[str], None] = lambda name: None
    source_started: Callable[[str], None] = lambda name: None
    source_done: Callable[[SourceResult], None] = lambda result: None
    item: Callable[[str, int], None] = lambda source, n: None
    evaluated: Callable[[int], None] = lambda n: None


@dataclass
class SearchSummary:
    crawl: list[SourceResult] = field(default_factory=list)
    prepared: PrepareStats | None = None
    evaluation: EvalStats | None = None
    matches: dict[str, int] = field(default_factory=dict)
    engine: str = ""
    notes: list[str] = field(default_factory=list)


def crawl_stage(
    db: Database,
    settings: Settings,
    search_cfg: SearchConfig,
    sources: list[str],
    max_pages: int | None = None,
    hooks: SearchHooks | None = None,
) -> list[SourceResult]:
    hooks = hooks or SearchHooks()
    results: list[SourceResult] = []
    for name in sources:
        hooks.source_started(name)
        part = run_crawl([name], search_cfg, settings, db, max_pages=max_pages, on_item=hooks.item)
        results += part
        hooks.source_done(part[0])
    return results


def run_search(
    db: Database,
    settings: Settings,
    base_cfg: SearchConfig,
    scoring: ScoringConfig,
    juniors: list[JuniorProfile],
    filters: SearchFilters,
    hooks: SearchHooks | None = None,
) -> SearchSummary:
    """crawl -> evaluate -> match, reusing the same stages as the CLI."""
    hooks = hooks or SearchHooks()
    cfg = apply_search_filters(base_cfg, filters)
    engine_name, note = resolve_engine(filters.engine, settings)
    summary = SearchSummary(engine=engine_name, notes=[note] if note else [])
    hooks.stage("Rastreando")
    summary.crawl = crawl_stage(db, settings, cfg, filters.sources, filters.max_pages, hooks)
    hooks.stage("Evaluando")
    summary.prepared, summary.evaluation = evaluate_stage(
        db, settings, cfg, engine_name, on_progress=hooks.evaluated
    )
    if filters.match_juniors and juniors and not summary.evaluation.stopped_reason:
        hooks.stage("Emparejando")
        scoring = scoring.model_copy(update={"junior_salary_max": filters.junior_salary_max})
        results = match_stage(db, settings, scoring, engine_name, juniors)
        summary.matches = {slug: len(matches) for slug, (matches, _stats) in results.items()}
    return summary


# ---------------------------------------------------------------------------------------------
# Stored offers as the UI sees them (score + bucket computed on the fly, nothing duplicated).
# ---------------------------------------------------------------------------------------------

_REASONS_ES = [
    (r"title contains '(.+)'", "El título contiene '{0}'"),
    (r"title is not a programming role \('(.+)'\)", "No es un puesto de programación ('{0}')"),
    (r"requires (\d+)\+ years of experience", "Pide {0}+ años de experiencia"),
    (r"published (\d+) days ago", "Publicada hace {0} días"),
    (r"company '(.+)' is blacklisted", "Empresa excluida: {0}"),
    (r"duplicate offer", "Oferta duplicada"),
]


def reason_es(reason: str | None) -> str:
    for pattern, template in _REASONS_ES:
        if m := re.fullmatch(pattern, reason or ""):
            return template.format(*m.groups())
    return reason or ""


@dataclass
class OfferView:
    offer: JobOffer
    status: str
    bucket: str
    score: float | None
    notes: list[str]
    engine: str
    label: str | None
    matches: list[Match]
    techs: list[str]


def detect_techs(offer: JobOffer, limit: int = 6) -> list[str]:
    if offer.skills:
        return offer.skills[:limit]
    text = strip_accents(f"{offer.title} {offer.description_md}").lower()
    return [t for t in TECH_SUGGESTIONS if _has_term(text, t)][:limit]


def _has_term(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<![\w+#]){re.escape(strip_accents(term).lower())}(?![\w+#])", text))


def list_offer_views(db: Database, scoring: ScoringConfig, now: datetime | None = None) -> list[OfferView]:
    """Every stored offer except duplicates. Evaluated ones get score+bucket; prefiltered ones are discards."""
    decisions: dict[str, OfferDecision] = {}
    for d in db.all_decisions():
        if d.question_set_version in OFFER_VERSIONS:
            best = decisions.get(d.offer_id)
            if best is None or d.created_at > best.created_at:
                decisions[d.offer_id] = d
    labels = db.labels()
    by_offer: dict[str, list[Match]] = {}
    for m in db.all_matches():
        by_offer.setdefault(m.offer_id, []).append(m)
    views = []
    for row in db.offer_rows():
        if row.status == STATUS_DUPLICATE:
            continue
        offer = JobOffer.model_validate_json(row.data)
        decision = decisions.get(offer.id)
        if row.status == STATUS_PREFILTERED:
            views.append(OfferView(offer, row.status, Bucket.DISCARD.value, None, [reason_es(row.discard_reason)],
                                   "reglas", labels.get(offer.id), [], detect_techs(offer)))  # fmt: skip
        elif decision is not None:
            scored = score_offer(offer, decision, scoring, now)
            views.append(OfferView(offer, row.status, scored.bucket.value, scored.score, scored.notes,
                                   decision.engine, labels.get(offer.id), by_offer.get(offer.id, []),
                                   detect_techs(offer)))  # fmt: skip
    views.sort(key=lambda v: (v.score is not None, v.score or 0), reverse=True)
    return views


class ResultFilters(BaseModel):
    q: str = ""
    techs: list[str] = Field(default_factory=list)
    provinces: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    work_mode: str = ""
    bucket: str = "top"  # top | review | discard | all
    salary_min: int | None = None
    label: str = ""  # "" | junior | no_junior | none
    junior: str = ""  # slug: only offers matched to that junior


def filter_offer_views(views: list[OfferView], f: ResultFilters) -> list[OfferView]:
    terms = [strip_accents(w).lower() for w in f.q.split() if w]
    out = []
    for v in views:
        o = v.offer
        if f.bucket != "all" and v.bucket != f.bucket:
            continue
        if f.sources and o.source not in f.sources:
            continue
        if f.work_mode and o.work_mode.value != f.work_mode:
            continue
        if f.salary_min and not ((o.salary_max or o.salary_min or 0) >= f.salary_min):
            continue
        if f.label == "none" and v.label:
            continue
        if f.label in ("junior", "no_junior") and v.label != f.label:
            continue
        if f.junior and not any(m.junior_slug == f.junior for m in v.matches):
            continue
        if f.provinces and not matches_location(o.location, o.work_mode.value, f.provinces):
            continue
        blob = strip_accents(f"{o.title} {o.company} {o.location} {' '.join(o.skills)} {o.description_md}").lower()
        if terms and not all(t in blob for t in terms):
            continue
        if f.techs and not any(_has_term(blob, t) for t in f.techs):
            continue
        out.append(v)
    return out


def bucket_counts(views: list[OfferView], f: ResultFilters) -> dict[str, int]:
    """Counts per bucket with every other filter applied (what the tabs show)."""
    counts = {}
    for bucket in ("top", "review", "discard"):
        counts[bucket] = len(filter_offer_views(views, f.model_copy(update={"bucket": bucket})))
    return counts


def save_junior(config_dir: Path, junior: JuniorProfile) -> Path:
    import yaml

    folder = config_dir / "juniors"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{junior.slug}.yaml"
    data = junior.model_dump(mode="json", exclude_defaults=False)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def apply_junior_threshold(config_dir: Path, value: float) -> Path:
    """Write `junior_target_min` into scoring.yaml (keeping a .bak copy and the file's comments).

    Buckets are computed on the fly from stored decisions, so nothing else needs re-running.
    """
    if not 0.05 <= value <= 0.95:
        raise ValueError("el umbral debe estar entre 0,05 y 0,95")
    path = config_dir / "scoring.yaml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text:
        path.with_name(path.name + ".bak").write_text(text, encoding="utf-8")
    line = f"junior_target_min: {value:g}"
    pattern = re.compile(r"^junior_target_min:[ \t]*[^\s#]+", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def match_single_junior(
    db: Database, settings: Settings, scoring: ScoringConfig, junior: JuniorProfile, engine_name: str = "heuristic"
) -> int:
    results = match_stage(db, settings, scoring, engine_name, [junior])
    return len(results[junior.slug][0])


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
