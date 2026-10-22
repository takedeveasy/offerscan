from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlencode

from scrapling.spiders import Request, Response

from offerscan.pipeline.normalize import strip_accents
from offerscan.scrapers.base import BaseJobSpider, html_to_text, register

BASE = "https://remotive.com"
# `candidate_required_location` values that include someone living in Spain.
_OPEN_TO_SPAIN = re.compile(r"\b(worldwide|anywhere|global|europe|emea|eu|cet|spain|espana)\b")


def search_url(keyword: str) -> str:
    return f"{BASE}/api/remote-jobs?{urlencode({'search': keyword, 'limit': 50})}"


def open_to_spain(required_location: str | None) -> bool:
    return bool(_OPEN_TO_SPAIN.search(strip_accents(required_location or "worldwide").lower()))


def parse_jobs(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for job in payload.get("jobs") or [] if isinstance(payload, dict) else []:
        if not job.get("id") or not open_to_spain(job.get("candidate_required_location")):
            continue
        salary = job.get("salary") or ""
        items.append(
            {
                "source": "remotive",
                "external_id": str(job["id"]),
                "url": job.get("url") or "",
                "title": job.get("title") or "",
                "company": job.get("company_name") or "",
                "location": f"Remoto ({job.get('candidate_required_location') or 'Worldwide'})",
                "work_mode_text": "remote",
                "salary_text": salary if "€" in salary or "eur" in salary.lower() else None,
                "contract_type": (job.get("job_type") or "").replace("_", " "),
                "published_at": job.get("publication_date"),
                "skills": job.get("tags") or [],
                "description": html_to_text(job.get("description") or ""),
            }
        )
    return items


@register
class RemotiveSpider(BaseJobSpider):
    """Public JSON API of remote jobs. Remotive asks for few calls a day, so keep it for occasional runs."""

    source_name = "remotive"

    def build_start_requests(self) -> list[Request]:
        return [Request(search_url(kw), sid="fast") for kw in self.search_cfg.keywords]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_jobs(response.json())
        self.logger.info("remotive API %s -> %d offers", response.url, len(items))
        for item in items:
            yield item
