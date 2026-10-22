from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote_plus, urljoin

from scrapling.parser import Selector
from scrapling.spiders import Request, Response

from offerscan.geo import Province, split_locations
from offerscan.scrapers.base import BaseJobSpider, decode_html, html_to_text, register

BASE = "https://www.tecnoempleo.com"
# Site filter `ex`: 1 = no experience, 2 = <1 year, 3 = 1 year, 4 = 2 years.
EXPERIENCE_FILTER = ",1,2,3,4,"
# Site filter `en_remoto`: 1 = 100% remote, 2 = on-site, 3 = hybrid.
WORK_MODE_IDS = {"remote": 1, "onsite": 2, "hybrid": 3}


def experience_filter(max_years: int) -> str:
    """`ex` ids for 0 / 1 / 2 years of maximum experience (verified on the real form)."""
    top = {0: 2, 1: 3}.get(max_years, 4)
    return "," + ",".join(str(i) for i in range(1, top + 1)) + ","


def search_url(keyword: str, provinces: list[Province], work_modes: list[str], max_years: int) -> str:
    """All filters are server-side: `pr` (province ids), `en_remoto` (work mode) and `ex` (experience)."""
    query = f"te={quote_plus(keyword)}&ex={experience_filter(max_years)}"
    if provinces:
        query += "&pr=," + ",".join(str(p.tecnoempleo_id) for p in provinces) + ","
    ids = sorted({WORK_MODE_IDS[m] for m in work_modes if m in WORK_MODE_IDS})
    if ids:
        query += "&en_remoto=," + ",".join(map(str, ids)) + ","
    return f"{BASE}/ofertas-trabajo/?{query}"
_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")


def _external_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _parts(fragment: str) -> list[str]:
    pieces = re.split(r"(?i)<br\s*/?>", fragment)
    texts = (html_to_text(p).replace("\n", " ").strip() for p in pieces)
    return [t for t in texts if t]


def parse_listing(html: str, base_url: str = BASE) -> tuple[list[dict[str, Any]], str | None]:
    """Parse a Tecnoempleo search results page into raw items + the next page URL."""
    page = Selector(html)
    items: list[dict[str, Any]] = []
    for card in page.css("div.p-3.border.rounded.mb-3.bg-white"):
        link = card.css("h3 a")
        if not link:
            continue
        url = urljoin(base_url, link[0].attrib.get("href", ""))
        title = (link[0].attrib.get("title") or link[0].get_all_text()).strip()
        company = card.css("a.link-muted")
        info_col = card.css("div.col-lg-3")
        parts = _parts(info_col[0].html_content) if info_col else []
        date_text = next((m.group(0) for p in parts if (m := _DATE_RE.search(p))), None)
        salary_text = next((p for p in parts if "€" in p or "&euro;" in p), None)
        bold = info_col[0].css("b") if info_col else []
        place = bold[0].get_all_text().strip() if bold else ""
        mode_text = next((p for p in parts if re.search(r"(?i)remot|h[ií]brid|presencial", p)), "")
        snippet_block = card.css("span.hidden-md-down")
        skills = [b.get_all_text().strip() for b in card.css("span.badge.mx-1")]
        items.append(
            {
                "source": "tecnoempleo",
                "external_id": _external_id(url),
                "url": url,
                "title": title,
                "company": company[0].get_all_text().strip() if company else "",
                "location": place,
                "work_mode_text": mode_text,
                "salary_text": salary_text,
                "published_at": date_text,
                "skills": skills,
                "description": _snippet(snippet_block[0].html_content) if snippet_block else "",
            }
        )
    nxt = next(
        (a.attrib["href"] for a in page.css("ul.pagination a.page-link") if "siguiente" in a.get_all_text()),
        None,
    )
    return items, urljoin(base_url, nxt.replace("&amp;", "&")) if nxt else None


def _snippet(fragment: str) -> str:
    text = html_to_text(re.sub(r'(?is)<span class="badge.*?</span>', "", fragment))
    return text


def parse_detail(html: str) -> dict[str, Any]:
    """Extract description, salary and metadata from a Tecnoempleo offer page."""
    page = Selector(html)
    out: dict[str, Any] = {}
    ld = _json_ld(page)
    block = page.css('div[itemprop="description"] div.fs--16')
    if block:
        out["description"] = html_to_text(block[0].html_content)
    elif ld.get("description"):
        out["description"] = str(ld["description"])
    if ld.get("datePosted"):
        out["published_at"] = ld["datePosted"]
    if ld.get("employmentType"):
        out["contract_type"] = str(ld["employmentType"]).replace("_", " ").title()
    salary = (ld.get("baseSalary") or {}).get("value") or {}
    if salary.get("minValue") or salary.get("maxValue"):
        lo, hi = salary.get("minValue"), salary.get("maxValue")
        out["salary_text"] = f"{lo}€ - {hi}€ b/a" if lo and hi else f"{lo or hi}€ b/a"
    if ld.get("jobLocationType") == "TELECOMMUTE":
        out["work_mode_text"] = "remoto"
    # The page is full of portal-wide technology links; only the row marked with the code icon is the offer's own.
    skills: list[str] = []
    for row in page.css("div.mt-5 div.d-flex.py-2"):
        if row.css("i.fi-code"):
            skills = [b.get_all_text().strip() for b in row.css("button") if b.get_all_text().strip()]
            break
    if skills:
        out["skills"] = skills
    return out


def _json_ld(page: Selector) -> dict[str, Any]:
    for script in page.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text, strict=False)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return {}


@register
class TecnoempleoSpider(BaseJobSpider):
    source_name = "tecnoempleo"

    def build_start_requests(self) -> list[Request]:
        cfg = self.search_cfg
        provinces, remote, _unknown = split_locations(cfg.locations)
        modes = cfg.work_modes or (["remote"] if remote and not provinces else [])
        return [
            Request(
                search_url(kw, provinces, modes, cfg.prefilter.max_experience_years),
                sid="fast",
                meta={"page": 1},
            )
            for kw in cfg.keywords
        ]

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items, next_url = parse_listing(decode_html(response.body), response.url)
        self.logger.info("listing %s -> %d offers", response.url, len(items))
        for item in items:
            if item["url"] and self.want_detail(item):
                yield Request(
                    item["url"], sid="fast", callback=self.parse_detail_response, meta={"item": item}
                )
            else:
                yield item
        page = response.meta.get("page", 1)
        if next_url and page < self.max_pages:
            yield Request(next_url, sid="fast", meta={"page": page + 1})

    async def parse_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        extra = parse_detail(decode_html(response.body))
        for key, value in extra.items():
            if value:
                item[key] = value
        yield item
