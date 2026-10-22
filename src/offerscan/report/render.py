from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, PackageLoader

from offerscan.models import Bucket, JuniorProfile, Match
from offerscan.ranking.offer_score import latest_offer_decision, score_offer
from offerscan.settings import ScoringConfig
from offerscan.storage.db import Database

FORMATS = ("html", "md", "csv", "jsonl")


@dataclass
class ReportRow:
    rank: int
    title: str
    company: str
    location: str
    work_mode: str
    salary: str
    source: str
    url: str
    published: str
    offer_score: float
    fit_score: float
    final_score: float
    bucket: str
    mentoring: bool
    reasons: list[str] = field(default_factory=list)


def _salary(lo: int | None, hi: int | None) -> str:
    def fmt(n: int) -> str:
        return f"{n:,}".replace(",", ".")

    if lo and hi and lo != hi:
        return f"{fmt(lo)}–{fmt(hi)} €/año"
    value = lo or hi
    return f"{fmt(value)} €/año" if value else ""


def build_rows(
    db: Database, matches: list[Match], cfg: ScoringConfig, now: datetime | None = None
) -> list[ReportRow]:
    rows: list[ReportRow] = []
    for match in sorted(matches, key=lambda m: m.final_score, reverse=True):
        offer = db.get_offer(match.offer_id)
        decision = latest_offer_decision(db, match.offer_id)
        if offer is None or decision is None:
            continue
        scored = score_offer(offer, decision, cfg, now)
        rows.append(
            ReportRow(
                rank=len(rows) + 1,
                title=offer.title,
                company=offer.company,
                location=offer.location,
                work_mode=offer.work_mode.value,
                salary=_salary(offer.salary_min, offer.salary_max),
                source=offer.source,
                url=offer.url,
                published=offer.published_at.date().isoformat() if offer.published_at else "",
                offer_score=scored.score,
                fit_score=match.fit_score,
                final_score=match.final_score,
                bucket=match.bucket.value,
                mentoring=scored.answers.get("mentorship", {}).get("noul", 0) >= 0.5,
                reasons=match.reasons,
            )
        )
    return rows


def _env() -> Environment:
    return Environment(
        loader=PackageLoader("offerscan.report", "templates"),
        autoescape=lambda name: bool(name and ".html" in name),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_report(
    junior: JuniorProfile, rows: list[ReportRow], fmt: str, engine: str, generated: datetime | None = None
) -> str:
    generated = generated or datetime.now(timezone.utc)
    ctx = {
        "junior": junior,
        "top": [r for r in rows if r.bucket == Bucket.TOP.value],
        "review": [r for r in rows if r.bucket == Bucket.REVIEW.value],
        "total": len(rows),
        "engine": engine,
        "generated": generated.strftime("%Y-%m-%d %H:%M UTC"),
    }
    return _env().get_template(f"report.{fmt}.j2").render(**ctx)


def write_reports(
    out_dir: Path,
    junior: JuniorProfile,
    rows: list[ReportRow],
    formats: list[str],
    engine: str,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fmt in formats:
        path = out_dir / f"{junior.slug}.{fmt}"
        if fmt in ("html", "md"):
            path.write_text(render_report(junior, rows, fmt, engine), encoding="utf-8")
        elif fmt == "csv":
            _write_csv(path, rows)
        elif fmt == "jsonl":
            path.write_text(
                "".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
            )
        else:
            raise ValueError(f"formato desconocido: {fmt}")
        written.append(path)
    return written


def _write_csv(path: Path, rows: list[ReportRow]) -> None:
    names = list(ReportRow.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=names)
        writer.writeheader()
        for row in rows:
            data = asdict(row)
            data["reasons"] = " | ".join(row.reasons)
            writer.writerow(data)
