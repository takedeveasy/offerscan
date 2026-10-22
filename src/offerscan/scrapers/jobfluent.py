from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlencode, urljoin

from scrapling.parser import Selector
from scrapling.spiders import Request, Response

from offerscan.geo import split_locations
from offerscan.scrapers.base import BaseJobSpider, decode_html, html_to_text, register, slug

BASE = "https://www.jobfluent.com"
# The only Spanish city pages: any other `/es/empleos-<x>` (even `-espana`) silently shows Barcelona.
CITIES = ("madrid", "barcelona", "valencia")
REMOTE = "remoto"
_ID_RE = re.compile(r"-([0-9a-f]{6})(?:\?|$)")


def search_url(keyword: str, city: str, page: int = 1) -> str:
    return f"{BASE}/es/empleos-{city}?{urlencode({'q': keyword, **({'page': page} if page > 1 else {})})}"


def cities_for(locations: list[str]) -> list[str]:
    """Supported city pages for the chosen places; no places means all of them."""
    provinces, remote, _unknown = split_locations(locations)
    if not provinces and not remote:
        return list(CITIES)
    wanted = [c for c in (slug(p.name) for p in provinces) if c in CITIES]
    return wanted + ([REMOTE] if remote else [])


def parse_listing(html: str, base_url: str = BASE) -> tuple[list[dict[str, Any]], str | None]:
    """Startup offers; the card title reads "<role> en <company>" and carries the skill labels."""
    page = Selector(html)
    items: list[dict[str, Any]] = []
    for card in page.css("div.panel-offer"):
        link = card.css("h3.offer-title a")
        if not link:
            continue
        url = urljoin(base_url, link[0].attrib.get("href", "")).split("?")[0]
        m = _ID_RE.search(url)
        if not m:
            continue
        heading = " ".join(link[0].get_all_text().split())
        logo = card.css("a.offer-logo img")
        company = logo[0].attrib.get("alt", "").removesuffix(" logo") if logo else ""
        title = heading.removesuffix(f" en {company}") if company else heading
        published = card.css("span.published-date")
        items.append(
            {
                "source": "jobfluent",
                "external_id": m.group(1),
                "url": url,
                "title": title,
                "company": company,
                "published_text": published[0].get_all_text().strip() if published else None,
                "skills": list(dict.fromkeys(a.get_all_text().strip() for a in card.css("a.label-skill"))),
            }
        )
    nxt = page.css('link[rel="next"]')
    return items, nxt[0].attrib.get("href") if nxt else None


def parse_detail(html: str) -> dict[str, Any]:
    """Offer pages use schema.org microdata (itemprop), not JSON-LD."""
    page = Selector(html)

    def prop(name: str) -> Selector | None:
        found = page.css(f'[itemprop="{name}"]')
        return found[0] if found else None

    out: dict[str, Any] = {}
    body = next((d for d in page.css('div[itemprop="description"]')), None)
    if body is not None:
        out["description"] = html_to_text(body.html_content)
    if (date := prop("datePosted")) is not None:
        out["published_at"] = date.attrib.get("content")
    if (kind := prop("employmentType")) is not None:
        out["contract_type"] = kind.get_all_text().strip()
    place = [p.get_all_text().strip() for p in (prop("addressLocality"), prop("addressRegion")) if p is not None]
    if place:
        out["location"] = ", ".join(dict.fromkeys(p for p in place if p))
    return out


@register
class JobFluentSpider(BaseJobSpider):
    """Startup jobs in Madrid, Barcelona, Valencia and remote; other provinces have no results here."""

    source_name = "jobfluent"

    def build_start_requests(self) -> list[Request]:
        return [
            Request(search_url(kw, city), sid="fast", meta={"page": 1, "city": city})
            for kw in self.search_cfg.keywords
            for city in cities_for(self.search_cfg.locations)
        ]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items, next_url = parse_listing(decode_html(response.body), response.url)
        self.logger.info("listing %s -> %d offers", response.url, len(items))
        city = response.meta.get("city", "")
        for item in items:
            if city in CITIES:  # the offer page has the exact place; this covers offers without a detail fetch
                item["location"] = city.title()
            if self.want_detail(item):
                yield Request(item["url"], sid="fast", callback=self.parse_detail_response, meta={"item": item})
            else:
                yield item
        page = response.meta.get("page", 1)
        if next_url and items and page < self.max_pages:
            yield Request(urljoin(BASE, next_url), sid="fast", meta={**response.meta, "page": page + 1})

    async def parse_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        item.update({k: v for k, v in parse_detail(decode_html(response.body)).items() if v})
        yield item
