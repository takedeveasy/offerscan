from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

from scrapling.spiders import Request, Response

from offerscan.geo import split_locations
from offerscan.scrapers.base import BaseJobSpider, decode_html, embedded_json, html_to_text, register, slug

BASE = "https://es.jooble.org"
STATE = r"window\.__INITIAL_STATE__\s*=\s*"
MAX_LOCATIONS = 3


def search_url(keyword: str, location: str = "") -> str:
    """SEO paths (`/trabajo-<kw>/<Place>`); robots.txt disallows the `/SearchResult` query form."""
    url = f"{BASE}/trabajo-{slug(keyword)}"
    return f"{url}/{quote(location)}" if location else url


def parse_listing(html: str) -> list[dict[str, Any]]:
    """Jobs live in the Redux state (`serpJobs.jobs[].items`), mixed with ad widgets that have `componentName`."""
    state = embedded_json(html, STATE) or {}
    pages = (state.get("serpJobs") or {}).get("jobs") or []
    items: list[dict[str, Any]] = []
    for job in (j for page in pages for j in page.get("items") or []):
        if "componentName" in job or not job.get("uid"):
            continue
        tags = [t.get("text", "") for t in job.get("tags") or [] if t.get("text")]
        items.append(
            {
                "source": "jooble",
                "external_id": str(job["uid"]),
                "url": job.get("url", "").split("?")[0],
                "title": (job.get("position") or "").strip(),
                "company": (job.get("company") or {}).get("name") or "",
                "location": (job.get("location") or {}).get("name") or "",
                "work_mode_text": "remoto" if job.get("isRemoteJob") else "",
                "salary_text": job.get("salary") or None,
                "contract_type": ", ".join(tags),
                "published_at": job.get("dateUpdated"),
                "description": html_to_text(job.get("fullContent") or job.get("content") or ""),
            }
        )
    return items


@register
class JoobleSpider(BaseJobSpider):
    """Aggregator: full descriptions come in the listing state, so no detail pages are fetched."""

    source_name = "jooble"

    def build_start_requests(self) -> list[Request]:
        provinces, _remote, _unknown = split_locations(self.search_cfg.locations)
        places = [p.name for p in provinces[:MAX_LOCATIONS]] or [""]
        return [Request(search_url(kw, place), sid="fast") for kw in self.search_cfg.keywords for place in places]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_listing(decode_html(response.body))
        self.logger.info("listing %s -> %d offers", response.url, len(items))
        for item in items:
            yield item
