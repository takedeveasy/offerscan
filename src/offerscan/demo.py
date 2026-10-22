"""Offline input for `offerscan run --from-fixtures`: real parsers over saved portal pages + synthetic demo offers."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from offerscan.scrapers import infojobs, linkedin, tecnoempleo
from offerscan.scrapers.base import decode_html

FIXTURES_RECORDED_ON = date(2026, 10, 21)


def _read(path: Path) -> str:
    return decode_html(path.read_bytes())


def load_fixture_raws(root: Path, today: date | None = None) -> list[dict[str, Any]]:
    """Parse every fixture under `root` with the real adapters' parsing functions (no network)."""
    raws: list[dict[str, Any]] = []
    te = root / "tecnoempleo"
    if (te / "listing.html").exists():
        items, _ = tecnoempleo.parse_listing(_read(te / "listing.html"))
        if (te / "detail.html").exists() and items:
            items[0].update({k: v for k, v in tecnoempleo.parse_detail(_read(te / "detail.html")).items() if v})
        raws += items
    li = root / "linkedin"
    if (li / "listing.html").exists():
        items = linkedin.parse_listing(_read(li / "listing.html"))
        if (li / "detail.html").exists() and items:
            items[0].update({k: v for k, v in linkedin.parse_detail(_read(li / "detail.html")).items() if v})
        raws += items
    ij = root / "infojobs" / "api_list.synthetic.json"
    if ij.exists():
        items, _ = infojobs.parse_api_list(json.loads(ij.read_text(encoding="utf-8")))
        raws += items
    demo = root / "demo" / "offers.jsonl"
    if demo.exists():
        shift = (today or date.today()) - FIXTURES_RECORDED_ON
        for line in demo.read_text(encoding="utf-8").splitlines():
            if line.strip():
                raws.append(_shift_date(json.loads(line), shift))
    return raws


def _shift_date(raw: dict[str, Any], shift: timedelta) -> dict[str, Any]:
    if raw.get("published_at"):
        raw["published_at"] = (date.fromisoformat(raw["published_at"]) + shift).isoformat()
    return raw
