from __future__ import annotations

import base64
import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote, urlencode

from scrapling.parser import Selector
from scrapling.spiders import Request, Response

from offerscan.geo import split_locations
from offerscan.pipeline.normalize import strip_accents
from offerscan.scrapers.base import BaseJobSpider, decode_html, html_to_text, register

API_BASE = "https://api.infojobs.net/api"
LIST_URL = f"{API_BASE}/9/offer"
DETAIL_URL = f"{API_BASE}/7/offer/{{id}}"
WEB_BASE = "https://www.infojobs.net/ofertas-trabajo"
CATEGORY = "informatica-telecomunicaciones"
MAX_HTML_KEYWORDS = 3
MAX_HTML_PROVINCES = 3


def keyword_slug(keyword: str) -> str:
    """`React` -> react, `.NET` -> net, `C#` -> c%23 (the shape InfoJobs uses in its own links)."""
    flat = strip_accents(keyword).lower().strip().lstrip(".")
    return quote(re.sub(r"\s+", "-", flat), safe="-+")


def web_search_url(keyword: str, province_slug: str = "", page: int = 1) -> str:
    """Province and category are path segments (verified against the page state: provinceIds/categoryIds)."""
    parts = [WEB_BASE, *([province_slug] if province_slug else []), CATEGORY, keyword_slug(keyword)]
    url = "/".join(parts)
    return f"{url}?page={page}" if page > 1 else url


def _value(node: Any) -> str:
    """InfoJobs encodes enums as {"id": ..., "value": ...}."""
    if isinstance(node, dict):
        return str(node.get("value") or "")
    return str(node or "")


def parse_api_list(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Return (raw items, total pages) from `GET /api/9/offer`."""
    items = []
    for o in payload.get("items") or payload.get("offers") or []:
        if not o.get("id"):
            continue
        salary_parts = [_value(o.get("salaryMin")), _value(o.get("salaryMax"))]
        salary = " - ".join(p for p in salary_parts if p)
        period = _value(o.get("salaryPeriod"))
        description = o.get("salaryDescription") or ""
        items.append(
            {
                "source": "infojobs",
                "external_id": str(o["id"]),
                "url": o.get("link") or f"https://www.infojobs.net/oferta/{o['id']}",
                "title": o.get("title") or "",
                "company": (o.get("author") or {}).get("name", ""),
                "location": ", ".join(p for p in [o.get("city"), _value(o.get("province"))] if p),
                "work_mode_text": _value(o.get("teleworking")),
                "salary_text": f"{salary} {period}".strip() if salary else description or None,
                "contract_type": _value(o.get("contractType")),
                "published_at": o.get("published") or o.get("updated"),
                "description": o.get("requirementMin") or "",
                "experience_text": _value(o.get("experienceMin")),
            }
        )
    return items, int(payload.get("totalPages") or 1)


def parse_api_detail(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    parts = [payload.get("description"), payload.get("minRequirements"), payload.get("desiredRequirements")]
    text = "\n\n".join(html_to_text(str(p)) for p in parts if p)
    if text:
        out["description"] = text
    skills = [s.get("skill") for s in payload.get("skillsList") or [] if isinstance(s, dict)]
    if skills:
        out["skills"] = [s for s in skills if s]
    if payload.get("experienceMin"):
        out["experience_text"] = _value(payload["experienceMin"])
    return out


def parse_web_detail(html: str) -> dict[str, Any]:
    """Description + minimum experience from a public offer page ("Requisitos" and "Descripción" blocks)."""
    page = Selector(html)
    sections: dict[str, Any] = {}
    for article in page.css("article.ij-Box"):
        heading = article.css("h3")
        if heading:
            sections[heading[0].get_all_text().strip().split("\n")[0]] = article
    out: dict[str, Any] = {}
    desc = next((a for name, a in sections.items() if name.startswith("Descripci")), None)
    if desc is not None:
        body = html_to_text(re.sub(r"(?is)<h3.*?</h3>", "", desc.html_content))
        if body:
            out["description"] = body
    req = sections.get("Requisitos")
    if req is not None:
        for dt in req.css("dt"):
            if dt.get_all_text().strip().lower().startswith("experiencia"):
                dd = dt.next
                if dd is not None and dd.get_all_text().strip():
                    out["experience_text"] = dd.get_all_text().strip()
    return out


def parse_web_listing(html: str) -> list[dict[str, Any]]:
    """Parse the public search page (as rendered through the stealth session)."""
    items: list[dict[str, Any]] = []
    for card in Selector(html).css("li.ij-OfferList-offerCardItem"):
        link = card.css("a.ij-OfferCardContent-description-link")
        if not link:  # ad slots share the list
            continue
        url = "https:" + link[0].attrib["href"] if link[0].attrib["href"].startswith("//") else link[0].attrib["href"]
        url = url.split("?")[0]
        title = card.css("span.ij-OfferCardContent-description-title-link")
        company = card.css("a.ij-OfferCardContent-description-subtitle-link")
        place = card.css("span.ij-OfferCardContent-description-list-item-truncate")
        salary = card.css("span.ij-OfferCardContent-description-salary-info")
        since = card.css("span.ij-FormatterSincedate")
        tags = [li.get_all_text().strip() for li in card.css("li.ij-OfferCardContent-description-list-item")]
        items.append(
            {
                "source": "infojobs",
                "external_id": url.rstrip("/").rsplit("/", 1)[-1],
                "url": url,
                "title": title[0].get_all_text().strip() if title else "",
                "company": company[0].get_all_text().strip() if company else "",
                "location": place[0].get_all_text().strip() if place else "",
                "work_mode_text": " ".join(tags),
                "salary_text": salary[0].get_all_text().strip() if salary else None,
                "contract_type": next((x for x in tags if x.lower().startswith("contrato")), ""),
                "published_text": since[0].get_all_text().strip() if since else None,
            }
        )
    return items


@register
class InfoJobsSpider(BaseJobSpider):
    """Official API when credentials exist; otherwise HTML with escalation to a stealth browser."""

    source_name = "infojobs"
    stealth_fallback = True

    @property
    def use_api(self) -> bool:
        s = self.settings
        return bool(s.infojobs_client_id and s.infojobs_client_secret)

    def _auth_headers(self) -> dict[str, str]:
        s = self.settings
        secret = s.infojobs_client_secret.get_secret_value() if s.infojobs_client_secret else ""
        token = base64.b64encode(f"{s.infojobs_client_id}:{secret}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def build_start_requests(self) -> list[Request]:
        if self.use_api:
            return [
                self._api_request(kw, province, 1)
                for kw in self.search_cfg.keywords
                for province in self._provinces()
            ]
        self.logger.warning(
            "InfoJobs: sin INFOJOBS_CLIENT_ID/SECRET se usa el HTML publico (suele mostrar captcha)."
        )
        provinces, _remote, _unknown = split_locations(self.search_cfg.locations)
        slugs = [p.infojobs_slug for p in provinces[:MAX_HTML_PROVINCES]] or [""]
        return [
            Request(web_search_url(kw, slug), sid="fast", callback=self.parse_web_response, meta={"page": 1})
            for kw in self.search_cfg.keywords[:MAX_HTML_KEYWORDS]
            for slug in slugs
        ]

    def _provinces(self) -> list[str]:
        """API province keys use the same slugs as the web paths ("las-palmas", "a-coruna")."""
        provinces, _remote, _unknown = split_locations(self.search_cfg.locations)
        return [p.infojobs_slug for p in provinces] or [""]

    def _api_request(self, keyword: str, province: str, page: int) -> Request:
        params = {
            # No "junior" in the query: many entry-level offers only say "sin experiencia". Seniority is
            # filtered downstream (prefilter + engine), same as the HTML path.
            "q": keyword,
            "category": CATEGORY,
            "sinceDate": "_30_DAYS",
            "order": "updated-desc",
            "page": page,
            "maxResults": 20,
        }
        if province:
            params["province"] = province
        return Request(
            f"{LIST_URL}?{urlencode(params)}",
            sid="fast",
            callback=self.parse_listing_response,
            headers=self._auth_headers(),
            meta={"page": page, "keyword": keyword, "province": province},
        )

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        if response.status in (401, 403):
            self.logger.error("InfoJobs API: credenciales invalidas (HTTP %s)", response.status)
            return
        payload = response.json()
        items, total_pages = parse_api_list(payload if isinstance(payload, dict) else {})
        self.logger.info("infojobs API -> %d offers", len(items))
        for item in items:
            if self.want_detail(item):
                yield Request(
                    DETAIL_URL.format(id=item["external_id"]),
                    sid="fast",
                    callback=self.parse_detail_response,
                    headers=self._auth_headers(),
                    meta={"item": item},
                )
            else:
                yield item
        page = response.meta.get("page", 1)
        if page < min(total_pages, self.max_pages):
            yield self._api_request(response.meta["keyword"], response.meta["province"], page + 1)

    async def parse_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        if response.status == 200:
            item.update({k: v for k, v in parse_api_detail(response.json()).items() if v})
        yield item

    async def parse_web_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_web_listing(decode_html(response.body))
        for item in items:
            if item["url"] and self.want_detail(item):
                yield Request(
                    item["url"],
                    sid="stealth",
                    callback=self.parse_web_detail_response,
                    meta={"item": item},
                    dont_filter=True,
                )
            else:
                yield item
        page = response.meta.get("page", 1)
        if items and page < self.max_pages:
            base = response.url.split("?")[0]
            yield Request(
                f"{base}?page={page + 1}",
                sid=response.request.sid,
                callback=self.parse_web_response,
                meta={"page": page + 1},
            )

    async def parse_web_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        if response.status == 200:
            item.update({k: v for k, v in parse_web_detail(decode_html(response.body)).items() if v})
        yield item

    async def retry_blocked_request(self, request: Request, response: Response) -> Request:
        if request.sid == "fast" and not self.use_api:
            self.logger.warning("InfoJobs bloqueado en sesion rapida; escalando a stealth")
            request.sid = "stealth"
        return request
