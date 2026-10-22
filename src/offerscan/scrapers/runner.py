from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from offerscan import browser
from offerscan.geo import matches_location
from offerscan.pipeline.normalize import detect_work_mode
from offerscan.pipeline.stages import ingest_raw
from offerscan.scrapers import REGISTRY
from offerscan.settings import SearchConfig, Settings
from offerscan.storage.db import Database

log = logging.getLogger("offerscan.crawl")


@dataclass
class SourceResult:
    source: str
    fetched: int = 0
    parsed: int = 0
    failed: int = 0
    blocked: int = 0
    stored: int = 0
    filtered: int = 0
    error: str = ""


async def _crawl_source(
    name: str,
    search_cfg: SearchConfig,
    settings: Settings,
    *,
    max_pages: int | None,
    dev: bool,
    on_item: Callable[[str, int], None] | None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[SourceResult, list[dict]]:
    result = SourceResult(name)
    items: list[dict] = []
    spider = REGISTRY[name](
        search_cfg,
        search_cfg.sources.get(name),
        max_pages=max_pages,
        dev=dev,
        data_dir=settings.data_dir,
        crawldir=settings.data_dir / "crawl" / name,
        settings=settings,
    )
    try:
        async for item in spider.stream():
            items.append(item)
            if on_item:
                on_item(name, len(items))
            if should_stop and should_stop():
                break
    except Exception as exc:  # one broken portal must never abort the whole run
        result.error = f"{type(exc).__name__}: {exc}"
        log.warning("%s fallo: %s", name, result.error)
    stats = spider.final_stats
    if stats is not None:
        result.fetched = stats.requests_count
        result.failed = stats.failed_requests_count
        result.blocked = stats.blocked_requests_count
    result.parsed = len(items)
    if spider.stealth_fallback and (result.blocked or result.failed or browser.is_missing_browser_error(result.error)):
        if not browser.is_installed():
            result.error = (
                f"El portal bloqueo la conexion rapida y falta el navegador stealth: ejecuta `{browser.INSTALL_HINT}`."
            )
    return result, items


# What each portal filters server-side; everything else is applied after scraping (see `post_filter`).
SERVER_SIDE_FILTERS: dict[str, dict[str, bool]] = {
    "tecnoempleo": {"provincias": True, "modalidad": True},
    "infojobs": {"provincias": True, "modalidad": False},
    "linkedin": {"provincias": False, "modalidad": False},
    "indeed": {"provincias": True, "modalidad": False},
    "glassdoor": {"provincias": False, "modalidad": False},
    "infoempleo": {"provincias": True, "modalidad": False},
    "trabajos": {"provincias": True, "modalidad": False},
    "jobfluent": {"provincias": False, "modalidad": False},
    "manfred": {"provincias": False, "modalidad": False},
    "talent": {"provincias": True, "modalidad": False},
    "jooble": {"provincias": True, "modalidad": False},
    "himalayas": {"provincias": False, "modalidad": True},
    "remotive": {"provincias": False, "modalidad": True},
}


def post_filter(source: str, items: list[dict], cfg: SearchConfig) -> list[dict]:
    """Apply the province / work-mode filters the portal could not apply itself."""
    server = SERVER_SIDE_FILTERS.get(source, {})
    check_place = bool(cfg.locations) and not server.get("provincias")
    check_mode = bool(cfg.work_modes) and not server.get("modalidad")
    if not (check_place or check_mode):
        return items
    kept = []
    for item in items:
        mode = detect_work_mode(item.get("work_mode_text"), item.get("location"), item.get("title")).value
        place = item.get("location") or ""
        if check_place and place and not matches_location(place, mode, cfg.locations):
            continue
        if check_mode and mode != "unknown" and mode not in cfg.work_modes:
            continue
        kept.append(item)
    return kept


def crawl(
    sources: list[str],
    search_cfg: SearchConfig,
    settings: Settings,
    db: Database,
    *,
    max_pages: int | None = None,
    dev: bool = False,
    on_item: Callable[[str, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[SourceResult]:
    """Crawl each source sequentially; persist raw JSONL, normalized offers and a run row."""
    results: list[SourceResult] = []
    raw_dir = settings.data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for name in sources:
        if should_stop and should_stop():
            break
        if name not in REGISTRY:
            results.append(SourceResult(name, error="adaptador desconocido"))
            continue
        result, items = asyncio.run(
            _crawl_source(
                name, search_cfg, settings, max_pages=max_pages, dev=dev, on_item=on_item, should_stop=should_stop
            )
        )
        kept = post_filter(name, items, search_cfg)
        result.filtered = len(items) - len(kept)
        items = kept
        if items:
            _write_jsonl(raw_dir / f"{name}-{stamp}.jsonl", items)
            result.stored = ingest_raw(db, items, search_cfg)
        db.add_run(
            name,
            fetched=result.fetched,
            parsed=result.parsed,
            failed=result.failed,
            blocked=result.blocked,
            notes=result.error,
        )
        results.append(result)
    return results


def _write_jsonl(path: Path, items: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
