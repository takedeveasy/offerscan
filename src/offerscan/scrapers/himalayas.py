from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from scrapling.spiders import Request, Response

from offerscan.scrapers.base import BaseJobSpider, html_to_text, register

BASE = "https://himalayas.app"


def search_url(keyword: str, entry_level: bool = True) -> str:
    """Remote jobs open to people living in Spain; `Entry-level` is Himalayas' own junior seniority."""
    params = {"q": keyword, "country": "Spain", **({"seniority": "Entry-level"} if entry_level else {})}
    return f"{BASE}/jobs/api/search?{urlencode(params)}"


def _iso(seconds: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_jobs(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for job in payload.get("jobs") or [] if isinstance(payload, dict) else []:
        url = job.get("guid") or job.get("applicationLink") or ""
        if not url:
            continue
        lo, hi = job.get("minSalary"), job.get("maxSalary")
        salary = None
        if (lo or hi) and job.get("currency") == "EUR":
            salary = f"{lo}€ - {hi}€ b/a" if lo and hi else f"{lo or hi}€ b/a"
        countries = job.get("locationRestrictions") or []
        items.append(
            {
                "source": "himalayas",
                "external_id": url.rstrip("/").rsplit("/", 1)[-1],
                "url": url.split("?")[0],
                "title": job.get("title") or "",
                "company": job.get("companyName") or "",
                "location": "Remoto" + (f" ({', '.join(countries)})" if countries else ""),
                "work_mode_text": "remote",
                "salary_text": salary,
                "contract_type": job.get("employmentType") or "",
                "published_at": _iso(job.get("pubDate")),
                "experience_text": ", ".join(job.get("seniority") or []),
                "description": html_to_text(job.get("description") or job.get("excerpt") or ""),
            }
        )
    return items


@register
class HimalayasSpider(BaseJobSpider):
    """Public JSON API (remote only, mostly in English); the search already filters by Spain and seniority."""

    source_name = "himalayas"

    def build_start_requests(self) -> list[Request]:
        entry = self.search_cfg.prefilter.max_experience_years <= 2
        return [Request(search_url(kw, entry), sid="fast") for kw in self.search_cfg.keywords]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_jobs(response.json())
        self.logger.info("himalayas API %s -> %d offers", response.url, len(items))
        for item in items:
            yield item
