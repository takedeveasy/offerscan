from __future__ import annotations

import html as htmllib
import json
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, ClassVar

from scrapling.fetchers import AsyncStealthySession, FetcherSession
from scrapling.spiders import Request, Response, Spider
from scrapling.spiders.result import CrawlStats
from scrapling.spiders.session import SessionManager

from offerscan.settings import SearchConfig, Settings, SourceConfig

REGISTRY: dict[str, type[BaseJobSpider]] = {}

BLOCK_MARKERS = ("captcha", "distil", "access denied", "unusual traffic", "are you a robot", "authwall")


def register(cls: type[BaseJobSpider]) -> type[BaseJobSpider]:
    REGISTRY[cls.source_name] = cls
    return cls


def decode_html(body: bytes) -> str:
    """Portals mix encodings; try UTF-8 first and fall back to cp1252."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def html_to_text(fragment: str) -> str:
    """Tiny HTML -> markdown-ish text converter (keeps lists and paragraph breaks)."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", fragment)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|ul|ol)>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = htmllib.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def looks_blocked(html: str) -> bool:
    head = html[:6000].lower()
    return any(marker in head for marker in BLOCK_MARKERS)


def slug(text: str) -> str:
    """`A Coruña` -> a-coruna (the shape most portals use in their location paths)."""
    from offerscan.pipeline.normalize import strip_accents

    return re.sub(r"[^a-z0-9]+", "-", strip_accents(text).lower()).strip("-")


def mentions_keyword(text: str, keywords: list[str]) -> bool:
    """For portals without server-side search: keep offers that mention any searched keyword."""
    from offerscan.pipeline.normalize import strip_accents

    flat = strip_accents(text).lower()
    return not keywords or any(
        re.search(rf"(?<![\w.#+]){re.escape(strip_accents(k).lower())}(?![\w#+])", flat) for k in keywords
    )


def embedded_json(html: str, start: str) -> Any:
    """Decode the JSON value that follows the `start` regex (e.g. `window.__INITIAL_STATE__ = `)."""
    m = re.search(start, html)
    if not m:
        return None
    try:
        return json.JSONDecoder().raw_decode(html, m.end())[0]
    except ValueError:
        return None


def job_posting_ld(html: str) -> dict[str, Any]:
    """First schema.org JobPosting in the page's JSON-LD (plain, inside a list or inside `@graph`)."""
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            data = json.loads(m.group(1), strict=False)
        except ValueError:
            continue
        if isinstance(data, dict):
            data = [data, *(data.get("@graph") or [])]
        nodes = data if isinstance(data, list) else []
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                return node
    return {}


def ld_detail(html: str) -> dict[str, Any]:
    """Raw-item fields from a JobPosting JSON-LD block (description, date, salary, contract, remote)."""
    ld = job_posting_ld(html)
    out: dict[str, Any] = {}
    if ld.get("description"):
        out["description"] = html_to_text(htmllib.unescape(str(ld["description"])))
    if ld.get("datePosted"):
        out["published_at"] = str(ld["datePosted"])
    if ld.get("employmentType"):
        kind = ld["employmentType"]
        out["contract_type"] = ", ".join(kind) if isinstance(kind, list) else str(kind).replace("_", " ").title()
    experience = ld.get("experienceRequirements")
    if isinstance(experience, dict):
        months = experience.get("monthsOfExperience")
        out["experience_text"] = f"{months} meses" if months is not None else str(experience.get("description") or "")
    elif experience:
        out["experience_text"] = html_to_text(str(experience))
    salary = ld.get("baseSalary") or {}
    value = salary.get("value") if isinstance(salary, dict) else None
    if isinstance(value, dict) and (salary.get("currency") or value.get("currency") or "EUR") == "EUR":
        lo, hi = value.get("minValue") or value.get("value"), value.get("maxValue")
        period = " al mes" if str(value.get("unitText", "")).upper() == "MONTH" else " b/a"
        if lo or hi:
            out["salary_text"] = (f"{lo}€ - {hi}€" if lo and hi else f"{lo or hi}€") + period
    if ld.get("jobLocationType") == "TELECOMMUTE":
        out["work_mode_text"] = "remoto"
    return out


class BaseJobSpider(Spider):
    """Common wiring for portal adapters.

    Subclasses implement `build_start_requests`, `parse_listing_response` and (optionally)
    `parse_detail_response`. They yield raw dicts consumed by `pipeline.normalize.normalize_raw`.
    """

    source_name: ClassVar[str]
    # Portals that block the fast HTTP session (Indeed, Glassdoor) retry blocked pages in a stealth browser.
    stealth_fallback: ClassVar[bool] = False
    concurrent_requests = 2
    autothrottle_enabled = True
    autothrottle_start_delay = 1.0
    logging_level = 20  # INFO

    def __init__(
        self,
        search_cfg: SearchConfig,
        source_cfg: SourceConfig | None = None,
        *,
        max_pages: int | None = None,
        dev: bool = False,
        data_dir: Path = Path("data"),
        crawldir: Path | None = None,
        settings: Settings | None = None,
    ):
        self.name = self.source_name
        self.settings = settings or Settings()
        self.search_cfg = search_cfg
        self.source_cfg = source_cfg or SourceConfig()
        self.max_pages = max_pages or self.source_cfg.max_pages
        self.max_details = self.source_cfg.max_details
        self.details_requested = 0
        self.robots_txt_obey = self.source_cfg.robots_txt_obey
        self.download_delay = self.source_cfg.download_delay
        self.development_mode = dev
        self.development_cache_dir = str(data_dir / "dev_cache" / self.source_name)
        self.blocked_pages = 0
        self.final_stats: CrawlStats | None = None
        super().__init__(crawldir=crawldir)

    def configure_sessions(self, manager: SessionManager) -> None:
        manager.add("fast", FetcherSession(impersonate="chrome"))
        if self.stealth_fallback:
            manager.add("stealth", AsyncStealthySession(headless=True), lazy=True)

    async def retry_blocked_request(self, request: Request, response: Response) -> Request:
        if self.stealth_fallback and request.sid == "fast":
            self.logger.warning("%s bloqueado en sesion rapida; escalando a stealth", self.source_name)
            request.sid = "stealth"
        return request

    async def start_requests(self) -> AsyncGenerator[Request, None]:
        for request in self.build_start_requests():
            yield request

    async def on_close(self) -> None:
        self.final_stats = self.stats
        await super().on_close()

    def build_start_requests(self) -> list[Request]:
        raise NotImplementedError

    def want_detail(self, item: dict[str, Any] | None = None) -> bool:
        """Politeness cap: at most `max_details` detail pages per run, never for offers the prefilter drops."""
        if item is not None and self._prefiltered(item):
            return False
        if self.details_requested >= self.max_details:
            return False
        self.details_requested += 1
        return True

    def _prefiltered(self, item: dict[str, Any]) -> bool:
        from offerscan.pipeline.normalize import normalize_raw
        from offerscan.pipeline.prefilter import prefilter

        try:
            offer = normalize_raw({"source": self.source_name, "external_id": "x", "url": "", **item})
        except (KeyError, ValueError, TypeError):
            return False
        return prefilter(offer, self.search_cfg.prefilter) is not None

    async def is_blocked(self, response: Response) -> bool:
        if await super().is_blocked(response):
            return True
        if response.status == 200 and looks_blocked(decode_html(response.body)):
            self.blocked_pages += 1
            return True
        return False

    async def parse(self, response: Response) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        async for out in self.parse_listing_response(response):
            yield out

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        raise NotImplementedError
        yield
