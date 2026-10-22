from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urljoin

from scrapling.parser import Selector
from scrapling.spiders import Request, Response

from offerscan.scrapers.base import BaseJobSpider, decode_html, register, slug

BASE = "https://www.glassdoor.es"
SPAIN = "espana"
SPAIN_ID = "IN219"


def search_url(keyword: str) -> str:
    """`/Empleo/espana-<kw>-empleos-SRCH_IL.0,6_IN219_KO7,<end>.htm`: IL/KO are the char spans of place and keyword."""
    kw = slug(keyword)
    start = len(SPAIN) + 1
    return f"{BASE}/Empleo/{SPAIN}-{kw}-empleos-SRCH_IL.0,{len(SPAIN)}_{SPAIN_ID}_KO{start},{start + len(kw)}.htm"


def _text(card: Selector, css: str) -> str:
    found = card.css(css)
    return found[0].get_all_text().strip() if found else ""


def parse_listing(html: str, base_url: str = BASE) -> list[dict[str, Any]]:
    """Server-rendered job cards (`li[data-test=jobListing]`); later pages load with JavaScript only."""
    items: list[dict[str, Any]] = []
    for card in Selector(html).css('li[data-test="jobListing"]'):
        link = card.css('a[data-test="job-title"]')
        job_id = card.attrib.get("data-jobid")
        if not link or not job_id:
            continue
        age = _text(card, '[data-test="job-age"]').replace("+", "").strip()  # "24 h", "3 d", "30 d+"
        items.append(
            {
                "source": "glassdoor",
                "external_id": job_id,
                "url": urljoin(base_url, link[0].attrib.get("href", "")).split("&")[0],
                "title": link[0].get_all_text().strip(),
                "company": _text(card, '[class*="EmployerName"]'),
                "location": _text(card, '[data-test="emp-location"]'),
                "salary_text": _text(card, '[data-test="detailSalary"]') or None,
                "published_text": f"hace {age}" if age else None,
                "description": _text(card, '[data-test="descSnippet"]'),
            }
        )
    return items


@register
class GlassdoorSpider(BaseJobSpider):
    """Country-wide search (province ids are opaque); provinces and work mode are filtered after crawling."""

    source_name = "glassdoor"
    stealth_fallback = True

    def build_start_requests(self) -> list[Request]:
        return [Request(search_url(kw), sid="fast") for kw in self.search_cfg.keywords]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_listing(decode_html(response.body), response.url)
        self.logger.info("listing %s -> %d offers", response.url, len(items))
        for item in items:
            yield item
