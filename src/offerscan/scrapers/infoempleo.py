from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote_plus, urljoin

from scrapling.parser import Selector
from scrapling.spiders import Request, Response

from offerscan.geo import Province, split_locations
from offerscan.scrapers.base import BaseJobSpider, decode_html, html_to_text, ld_detail, register, slug

BASE = "https://www.infoempleo.com"
# Province path segments (`/trabajo/en_<slug>/`) that differ from the plain slug of the name.
PROVINCE_SLUGS = {
    "a-coruna": "a-corunna",
    "baleares": "islas-baleares",
    "bizkaia": "vizcaya",
    "gipuzkoa": "guipuzcoa",
}
_ID_RE = re.compile(r"/(\d+)/?$")


def province_slug(province: Province) -> str:
    key = slug(province.name)
    return PROVINCE_SLUGS.get(key, key)


def search_url(keyword: str, province: Province | None = None, page: int = 1, first_job: bool = False) -> str:
    """`/primer-empleo/` is the portal's own section for offers without experience."""
    section = "primer-empleo" if first_job else "trabajo"
    place = f"en_{province_slug(province)}/" if province else ""
    url = f"{BASE}/{section}/{place}?search={quote_plus(keyword)}"
    return f"{url}&pagina={page}" if page > 1 else url


def _text(card: Selector, css: str) -> str:
    found = card.css(css)
    return found[0].get_all_text().strip() if found else ""


def parse_listing(html: str, base_url: str = BASE) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for card in Selector(html).css("li.offerblock"):
        link = card.css("h2.offer-title a")
        if not link:
            continue
        url = urljoin(base_url, link[0].attrib.get("href", ""))
        m = _ID_RE.search(url)
        if not m:
            continue
        desc = card.css("p.offer-description")
        items.append(
            {
                "source": "infoempleo",
                "external_id": m.group(1),
                "url": url,
                "title": " ".join(link[0].get_all_text().split()),  # keywords come wrapped in <em>
                "company": _text(card, "p.company-name"),
                "location": _text(card, "p.offer-location"),
                "contract_type": _text(card, "p.offer-contract-fact"),
                "experience_text": _text(card, "p.offer-experience-fact"),
                "published_text": _text(card, "p.offer-date"),
                "description": html_to_text(re.sub(r"(?is)<footer.*?</footer>", "", desc[0].html_content))
                if desc
                else "",
            }
        )
    return items


@register
class InfoempleoSpider(BaseJobSpider):
    """Province is a path segment; with "0 years" of experience the search uses the "Primer empleo" section."""

    source_name = "infoempleo"

    def build_start_requests(self) -> list[Request]:
        cfg = self.search_cfg
        provinces, _remote, _unknown = split_locations(cfg.locations)
        first_job = cfg.prefilter.max_experience_years == 0
        return [
            Request(
                search_url(kw, province, first_job=first_job), sid="fast", meta={"page": 1, "kw": kw, "p": province}
            )
            for kw in cfg.keywords
            for province in (provinces or [None])
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
            first_job = "/primer-empleo/" in response.url
            meta = {**response.meta, "page": page + 1}
            yield Request(search_url(meta["kw"], meta["p"], page + 1, first_job), sid="fast", meta=meta)

    async def parse_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        item.update({k: v for k, v in ld_detail(decode_html(response.body)).items() if v})
        yield item
