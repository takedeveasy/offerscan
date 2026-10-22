from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from scrapling.spiders import Request, Response

from offerscan.scrapers.base import (
    BaseJobSpider,
    decode_html,
    embedded_json,
    html_to_text,
    ld_detail,
    mentions_keyword,
    register,
)

BASE = "https://www.getmanfred.com"
LIST_URL = f"{BASE}/api/v2/public/offers?lang=ES&onlyActive=true"


def work_mode_text(remote_percentage: Any) -> str:
    try:
        pct = int(remote_percentage)
    except (TypeError, ValueError):
        return ""
    return "remoto" if pct >= 100 else "hibrido" if pct > 0 else "presencial"


def parse_offers(payload: Any) -> list[dict[str, Any]]:
    """The public API returns every active offer (a few dozen, all tech, salary always stated)."""
    items: list[dict[str, Any]] = []
    for o in payload if isinstance(payload, list) else []:
        if not o.get("id") or o.get("status", "ACTIVE") != "ACTIVE":
            continue
        lo, hi = o.get("salaryFrom") or 0, o.get("salaryTo") or 0
        currency = o.get("currency") or "€"
        salary = None
        if (lo or hi) and currency in ("€", "EUR"):
            salary = f"{lo}€ - {hi}€ b/a" if lo and hi else f"{lo or hi}€ b/a"
        items.append(
            {
                "source": "manfred",
                "external_id": str(o["id"]),
                "url": f"{BASE}/ofertas-empleo/{o['id']}/{o.get('slug', '')}",
                "title": o.get("position") or "",
                "company": (o.get("company") or {}).get("name", ""),
                "location": "; ".join(o.get("locations") or []),
                "work_mode_text": work_mode_text(o.get("remotePercentage")),
                "salary_text": salary,
                "contract_type": "Freelance" if o.get("isFreelance") else "",
                "published_at": o.get("updatedAt"),
            }
        )
    return items


def parse_detail(html: str) -> dict[str, Any]:
    """The JSON-LD only has a teaser; tasks and the stack (must / nice / extra) live in `__NEXT_DATA__`."""
    out = ld_detail(html)
    out.pop("work_mode_text", None)  # JSON-LD says TELECOMMUTE even for hybrid; remotePercentage is exact
    data = embedded_json(html, r'<script id="__NEXT_DATA__" type="application/json">') or {}
    offer = ((data.get("props") or {}).get("pageProps") or {}).get("offer") or {}
    tasks = [r.get("item", "") for r in (offer.get("requirementsSectionData") or {}).get("responsabilities") or []]
    skills = ((offer.get("skillsSectionData") or {}).get("skills")) or {}
    stack = {k: [s.get("skill") for s in skills.get(k) or [] if s.get("skill")] for k in ("must", "nice", "extra")}
    parts = [out.get("description", "")]
    if tasks:
        parts.append("Responsabilidades:\n" + "\n".join(f"- {html_to_text(t).replace('**', '')}" for t in tasks))
    if stack["must"]:
        parts.append("Imprescindible: " + ", ".join(stack["must"]))
    if stack["nice"]:
        parts.append("Deseable: " + ", ".join(stack["nice"]))
    out["description"] = "\n\n".join(p for p in parts if p)
    out["skills"] = stack["must"] + stack["nice"] + stack["extra"]
    return out


@register
class ManfredSpider(BaseJobSpider):
    """No search endpoint: one API call lists everything and the keywords are matched against each offer."""

    source_name = "manfred"

    def build_start_requests(self) -> list[Request]:
        return [Request(LIST_URL, sid="fast", headers={"Accept": "application/json"})]

    def _wanted(self, item: dict[str, Any]) -> bool:
        text = f"{item['title']} {item.get('description', '')} {' '.join(item.get('skills') or [])}"
        return mentions_keyword(text, self.search_cfg.keywords)

    async def parse_listing_response(
        self, response: Response
    ) -> AsyncGenerator[dict[str, Any] | Request | None, None]:
        items = parse_offers(response.json())
        self.logger.info("manfred API -> %d offers", len(items))
        for item in items:
            if self.want_detail(item):
                yield Request(item["url"], sid="fast", callback=self.parse_detail_response, meta={"item": item})
            elif self._wanted(item):
                yield item

    async def parse_detail_response(self, response: Response) -> AsyncGenerator[dict[str, Any], None]:
        item = dict(response.meta["item"])
        item.update({k: v for k, v in parse_detail(decode_html(response.body)).items() if v})
        if self._wanted(item):
            yield item
