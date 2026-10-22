from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from scrapling.spiders import Request, Response

from offerscan.geo import split_locations
from offerscan.scrapers.base import BaseJobSpider, decode_html, embedded_json, html_to_text, register

BASE = "https://es.indeed.com"
JOBCARDS = r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*'
# Indeed's own facet id for "Teletrabajo" (taxonomyAttributes in the job cards).
REMOTE_FILTER = "0kf:attr(DSQF7);"
MAX_LOCATIONS = 3


def search_url(keyword: str, location: str = "", remote: bool = False) -> str:
    """Only the first page is public: `start=10` redirects anonymous visitors to a sign-in wall."""
    params = {"q": keyword, "l": location or "España"}
    if remote:
        params["sc"] = REMOTE_FILTER
    return f"{BASE}/jobs?{urlencode(params)}"


def _iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_listing(html: str) -> list[dict[str, Any]]:
    """Job cards come as JSON in `window.mosaic.providerData["mosaic-provider-jobcards"]`."""
    data = embedded_json(html, JOBCARDS) or {}
    model = (data.get("metaData") or {}).get("mosaicProviderJobCardsModel") or {}
    items: list[dict[str, Any]] = []
    for job in model.get("results") or []:
        jk = job.get("jobkey")
        if not jk or job.get("sponsored"):
            continue
        remote = job.get("remoteWorkModel") or {}
        salary = job.get("salarySnippet") or {}
        items.append(
            {
                "source": "indeed",
                "external_id": jk,
                "url": f"{BASE}/viewjob?jk={jk}",
                "title": job.get("displayTitle") or job.get("title") or "",
                "company": job.get("company") or "",
                "location": job.get("formattedLocation") or "",
                "work_mode_text": remote.get("text") or ("remoto" if job.get("remoteLocation") else ""),
                "salary_text": salary.get("text") if salary.get("currency") in ("EUR", "") else None,
                "published_at": _iso(job.get("pubDate")),
                "contract_type": ", ".join(job.get("jobTypes") or []),
                "description": html_to_text(job.get("snippet") or ""),
            }
        )
    return items


@register
class IndeedSpider(BaseJobSpider):
    """Listing only: offer pages answer 401 without a session, and the card snippet already lists requirements."""

    source_name = "indeed"
    stealth_fallback = True

    def build_start_requests(self) -> list[Request]:
        cfg = self.search_cfg
        provinces, remote, _unknown = split_locations(cfg.locations)
        only_remote = remote and not provinces or cfg.work_modes == ["remote"]
        places = [p.name for p in provinces[:MAX_LOCATIONS]] or [""]
        return [
            Request(search_url(kw, place, remote=only_remote), sid="fast")
            for kw in cfg.keywords
            for place in places
        ]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_listing(decode_html(response.body))
        self.logger.info("listing %s -> %d offers", response.url, len(items))
        for item in items:
            yield item
