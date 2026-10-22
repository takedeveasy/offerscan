from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote_plus

from scrapling.parser import Selector
from scrapling.spiders import Request, Response

from offerscan.geo import split_locations
from offerscan.scrapers.base import BaseJobSpider, decode_html, html_to_text, register, slug

BASE = "https://www.trabajos.com"


def search_url(keyword: str, province_slug: str = "") -> str:
    """Province pages are `/ofertas-empleo/<province>/<keyword>`; the national search takes `CADENA`."""
    if province_slug:
        return f"{BASE}/ofertas-empleo/{province_slug}/{slug(keyword)}"
    return f"{BASE}/ofertas-empleo/?CADENA={quote_plus(keyword)}"


def _text(card: Selector, css: str) -> str:
    found = card.css(css)
    return found[0].get_all_text().strip() if found else ""


def parse_listing(html: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for card in Selector(html).css("div.listado2014.oferta"):
        link = card.css("a.oferta")
        if not link or not link[0].attrib.get("data-j4m_val"):
            continue
        place = card.css("span.location")
        facts = [f.get_all_text().strip() for f in card.css("p.oi span")]
        salary = next((f for f in facts if "€" in f), None)
        items.append(
            {
                "source": "trabajos",
                "external_id": link[0].attrib["data-j4m_val"],
                "url": link[0].attrib.get("href", ""),
                "title": link[0].get_all_text().strip(),
                "company": _text(card, "a.empresa"),
                "location": " ".join(place[0].get_all_text().split()) if place else "",
                "published_at": _text(card, "span.fecha") or None,
                "salary_text": salary,
                "contract_type": ", ".join(f for f in facts if f != salary),
                "description": _text(card, "div.doextended"),
            }
        )
    return items


def parse_detail(html: str) -> dict[str, Any]:
    """Full text lives in `div.subtitulo`; salary/contract/experience in `div.detalle` rows (its JSON-LD is a stub)."""
    page = Selector(html)
    out: dict[str, Any] = {}
    body = page.css("div.subtitulo")
    if body:
        out["description"] = html_to_text(body[0].html_content)
    for row in page.css("div.bloqueoferta div.detalle"):
        label = row.css("div")
        value = row.css("strong")
        if not label or not value:
            continue
        name, text = label[0].get_all_text().strip().lower(), value[0].get_all_text().strip()
        if name.startswith("salario") and re.search(r"\d", text):
            out["salary_text"] = text
        elif name.startswith("tipo de contrato"):
            out["contract_type"] = text
        elif "experiencia" in name:
            out["experience_text"] = text
    return out


@register
class TrabajosSpider(BaseJobSpider):
    """One results page per keyword and province (the portal shows every match on a single page)."""

    source_name = "trabajos"

    def build_start_requests(self) -> list[Request]:
        provinces, _remote, _unknown = split_locations(self.search_cfg.locations)
        slugs = [slug(p.name) for p in provinces] or [""]
        return [Request(search_url(kw, s), sid="fast") for kw in self.search_cfg.keywords for s in slugs]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_listing(decode_html(response.body))
        self.logger.info("listing %s -> %d offers", response.url, len(items))
        for item in items:
            if item["url"] and self.want_detail(item):
                yield Request(item["url"], sid="fast", callback=self.parse_detail_response, meta={"item": item})
            else:
                yield item

    async def parse_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        item.update({k: v for k, v in parse_detail(decode_html(response.body)).items() if v})
        yield item
