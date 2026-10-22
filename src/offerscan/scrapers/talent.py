from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlencode, urljoin

from scrapling.parser import Selector
from scrapling.spiders import Request, Response

from offerscan.geo import split_locations
from offerscan.scrapers.base import BaseJobSpider, decode_html, ld_detail, register

BASE = "https://es.talent.com"
MAX_LOCATIONS = 3
_ID_RE = re.compile(r"[?&]id=(\d+)")


def search_url(keyword: str, location: str = "", page: int = 1) -> str:
    params = {"k": keyword, "l": location or "España", **({"p": page} if page > 1 else {})}
    return f"{BASE}/jobs?{urlencode(params)}"


def _text(card: Selector, css: str) -> str:
    found = card.css(css)
    return found[0].get_all_text().strip() if found else ""


def parse_listing(html: str, base_url: str = BASE) -> list[dict[str, Any]]:
    """Aggregated offers (`article.JobCard_card__*`, hashed CSS-module class names)."""
    items: list[dict[str, Any]] = []
    for card in Selector(html).css('article[class*="JobCard_card"]'):
        link = card.css('a[href*="view?id="]')
        m = _ID_RE.search(link[0].attrib.get("href", "")) if link else None
        if not m:
            continue
        snippet = card.css('p[class*="JobCard_snippet"]')
        text = snippet[0].get_all_text() if snippet else ""
        when = card.css("time")
        items.append(
            {
                "source": "talent",
                "external_id": m.group(1),
                "url": urljoin(base_url, f"/view?id={m.group(1)}"),
                "title": _text(card, 'h2[class*="JobCard_title"]'),
                "company": _text(card, 'span[class*="JobCard_company"]'),
                "location": _text(card, 'span[class*="JobCard_location"]').removesuffix(", Spain"),
                "published_at": when[0].attrib.get("datetime") if when else None,
                "description": text.replace(link[0].get_all_text(), "").strip(),
            }
        )
    return items


@register
class TalentSpider(BaseJobSpider):
    source_name = "talent"

    def build_start_requests(self) -> list[Request]:
        provinces, _remote, _unknown = split_locations(self.search_cfg.locations)
        places = [p.name for p in provinces[:MAX_LOCATIONS]] or [""]
        return [
            Request(search_url(kw, place), sid="fast", meta={"page": 1, "kw": kw, "place": place})
            for kw in self.search_cfg.keywords
            for place in places
        ]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_listing(decode_html(response.body), response.url)
        self.logger.info("listing %s -> %d offers", response.url, len(items))
        for item in items:
            if self.want_detail(item):
                yield Request(item["url"], sid="fast", callback=self.parse_detail_response, meta={"item": item})
            else:
                yield item
        page = response.meta.get("page", 1)
        if items and page < self.max_pages:
            meta = {**response.meta, "page": page + 1}
            yield Request(search_url(meta["kw"], meta["place"], page + 1), sid="fast", meta=meta)

    async def parse_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        item.update({k: v for k, v in ld_detail(decode_html(response.body)).items() if v})
        yield item
