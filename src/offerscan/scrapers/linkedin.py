from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote_plus

from scrapling.parser import Selector
from scrapling.spiders import Request, Response

from offerscan.scrapers.base import BaseJobSpider, decode_html, html_to_text, register

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
PAGE_SIZE = 10
_URN_RE = re.compile(r"jobPosting:(\d+)")


def _text(node: Selector | None) -> str:
    return node.get_all_text().strip() if node is not None else ""


def parse_listing(html: str) -> list[dict[str, Any]]:
    """Parse the guest `seeMoreJobPostings` fragment (list of <li> cards)."""
    items: list[dict[str, Any]] = []
    for card in Selector(html).css("div.base-search-card"):
        m = _URN_RE.search(card.attrib.get("data-entity-urn", ""))
        if not m:
            continue
        job_id = m.group(1)
        link = card.css("a.base-card__full-link")
        title = _text(card.css(".base-search-card__title")[0]) if card.css(".base-search-card__title") else ""
        company = card.css(".base-search-card__subtitle")
        location = card.css(".job-search-card__location")
        time_tag = card.css("time")
        salary = card.css(".job-search-card__salary-info")
        items.append(
            {
                "source": "linkedin",
                "external_id": job_id,
                "url": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title,
                "company": _text(company[0]) if company else "",
                "location": _text(location[0]) if location else "",
                "published_at": time_tag[0].attrib.get("datetime") if time_tag else None,
                "salary_text": _text(salary[0]) if salary else None,
                "raw_link": link[0].attrib.get("href", "").split("?")[0] if link else "",
            }
        )
    return items


def parse_detail(html: str) -> dict[str, Any]:
    page = Selector(html)
    out: dict[str, Any] = {}
    markup = page.css("div.show-more-less-html__markup")
    if markup:
        out["description"] = html_to_text(markup[0].html_content)
    criteria: dict[str, str] = {}
    for item in page.css("li.description__job-criteria-item"):
        key = _text(item.css(".description__job-criteria-subheader")[0]) if item.css(
            ".description__job-criteria-subheader"
        ) else ""
        value = _text(item.css(".description__job-criteria-text")[0]) if item.css(
            ".description__job-criteria-text"
        ) else ""
        if key:
            criteria[key] = value
    if criteria:
        out["criteria"] = criteria
        out["contract_type"] = criteria.get("Employment type", "")
        out["seniority_label"] = criteria.get("Seniority level", "")
    return out


@register
class LinkedInSpider(BaseJobSpider):
    """Public guest endpoints only (no login). Disabled by default; see config/search.yaml."""

    source_name = "linkedin"
    concurrent_requests = 1

    async def on_start(self, resuming: bool = False) -> None:
        self.logger.warning(
            "LinkedIn esta activado: su robots.txt y sus Terminos de Servicio restringen el scraping. "
            "Uso bajo tu propia responsabilidad."
        )
        await super().on_start(resuming)

    def build_start_requests(self) -> list[Request]:
        location = quote_plus(self.search_cfg.locations[0] if self.search_cfg.locations else "Spain")
        if location.lower() == "remoto":
            location = "Spain"
        urls = []
        for kw in self.search_cfg.keywords:
            query = f"keywords={quote_plus(kw + ' junior')}&location={location}&f_E=1%2C2&f_TPR=r604800"
            urls.append(f"{SEARCH_URL}?{query}&start=0")
        return [Request(u, sid="fast", meta={"page": 1}) for u in urls]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_listing(decode_html(response.body))
        self.logger.info("linkedin listing -> %d cards", len(items))
        for item in items:
            if self.want_detail(item):
                yield Request(
                    DETAIL_URL.format(id=item["external_id"]),
                    sid="fast",
                    callback=self.parse_detail_response,
                    meta={"item": item},
                )
            else:
                yield item
        page = response.meta.get("page", 1)
        if items and page < self.max_pages:
            start = page * PAGE_SIZE
            next_url = re.sub(r"start=\d+", f"start={start}", response.url)
            yield Request(next_url, sid="fast", meta={"page": page + 1})

    async def parse_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        item.update({k: v for k, v in parse_detail(decode_html(response.body)).items() if v})
        yield item
