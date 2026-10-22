from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as dateparser

from offerscan.models import JobOffer, WorkMode, make_offer_id

MIN_ANNUAL, MAX_ANNUAL = 6_000, 400_000

_NUM = r"\d{1,3}(?:[.\s]\d{3})+|\d+(?:[.,]\d+)?"
_AMOUNT_RE = re.compile(rf"(?P<n>{_NUM})\s*(?P<k>[kK](?![a-zA-Z]))?\s*(?P<cur>€|&euro;|eur\b|euros\b)?")


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _to_number(raw: str, is_k: bool) -> float:
    raw = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:[.\s]\d{3})+", raw):
        value = float(re.sub(r"[.\s]", "", raw))
    else:
        value = float(raw.replace(",", "."))
    return value * 1000 if is_k else value


def parse_salary(
    text: str | None, monthly_payments: int = 14
) -> tuple[int | None, int | None]:
    """Return annual (min, max) in euros. Monthly figures are multiplied by `monthly_payments`."""
    if not text:
        return None, None
    lowered = text.lower()
    if re.search(r"/\s*h\b|por hora|/hora|hourly|/hour", lowered):
        return None, None
    monthly = bool(re.search(r"/\s*mes|al mes|mensual|/month|per month|b/m|bruto/mes", lowered))
    values: list[float] = []
    for m in _AMOUNT_RE.finditer(text):
        is_k = bool(m.group("k"))
        has_cur = bool(m.group("cur"))
        number = _to_number(m.group("n"), is_k)
        if not (is_k or has_cur or number >= 1000):
            continue
        values.append(number)
    if not values:
        return None, None
    if monthly:
        values = [v * monthly_payments for v in values]
    values = [v for v in values if MIN_ANNUAL <= v <= MAX_ANNUAL]
    if not values:
        return None, None
    return int(min(values)), int(max(values))


_NUM_WORDS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6}
_YEARS = (
    r"(?P<n>\d{1,2}|un|una|uno|dos|tres|cuatro|cinco|seis)\s*(?P<plus>\+)?\s*"
    r"(?:a[nñ]os?|years?|yrs?)"
)
_YEARS_RE = re.compile(_YEARS, re.IGNORECASE)
_RANGE_RE = re.compile(
    r"(?P<a>\d{1,2})\s*(?:-|a|y|to)\s*(?P<b>\d{1,2})\s*(?:\+\s*)?(?:a[nñ]os?|years?)", re.IGNORECASE
)
_NO_EXP_RE = re.compile(
    r"sin\s+experiencia|no\s+(?:se\s+)?requiere\s+experiencia|no\s+experience|"
    r"experiencia\s+no\s+(?:requerida|necesaria)|no\s+previous\s+experience",
    re.IGNORECASE,
)


def parse_experience_years(text: str | None) -> int | None:
    """Minimum years of experience asked. Only counts numbers close to an experience keyword."""
    if not text:
        return None
    found: list[int] = []
    for m in _YEARS_RE.finditer(text):
        window = text[max(0, m.start() - 45) : m.end() + 45].lower()
        if "experien" not in window and "exp." not in window:
            continue
        before = text[max(0, m.start() - 12) : m.start()].lower()
        if re.search(r"menos de\s*$|less than\s*$|hasta\s*$|up to\s*$", before):
            found.append(0)
            continue
        raw = m.group("n").lower()
        n = int(raw) if raw.isdigit() else _NUM_WORDS[raw]
        range_m = _RANGE_RE.search(text, max(0, m.start() - 8))
        if range_m and range_m.end() == m.end():
            n = min(int(range_m.group("a")), int(range_m.group("b")))
        found.append(n)
    if found:
        return max(found)
    if _NO_EXP_RE.search(text):
        return 0
    return None


def detect_work_mode(*texts: str | None) -> WorkMode:
    blob = strip_accents(" ".join(t for t in texts if t)).lower()
    if re.search(r"hibrid|hybrid|semipresencial|remoto parcial", blob):
        return WorkMode.HYBRID
    if re.search(r"remot|teletrabaj|work from home|\bwfh\b|\bremote\b|100% a distancia", blob):
        return WorkMode.REMOTE
    if re.search(r"presencial|on-?site|in-?office|en oficina", blob):
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_REL_RE = re.compile(
    r"(?:hace\s+)?(?P<n>\d+|un|una)\s*(?P<u>minutos?|minutes?|min|horas?|hours?|h|d[ií]as?|days?|d|"
    r"semanas?|weeks?|sem|w|meses|mes|months?|a[nñ]os?|years?)\b(?:\s+ago)?",
    re.IGNORECASE,
)


def _relative_delta(unit: str, n: int) -> timedelta | None:
    if unit.startswith("min"):
        return timedelta(minutes=n)
    if unit.startswith("h"):
        return timedelta(hours=n)
    if unit.startswith("d"):
        return timedelta(days=n)
    if unit.startswith(("s", "w")):
        return timedelta(weeks=n)
    if unit.startswith("m"):
        return timedelta(days=30 * n)
    if unit.startswith(("a", "y")):
        return timedelta(days=365 * n)
    return None


def parse_date(text: str | None, now: datetime) -> datetime | None:
    """Parse absolute (ISO, dd/mm/yyyy) and relative ("hace 3 dias", "6 days ago") dates."""
    if not text:
        return None
    t = text.strip().lower()
    if not t:
        return None
    if t in {"hoy", "today", "just now", "ahora"}:
        return now
    if t in {"ayer", "yesterday"}:
        return now - timedelta(days=1)
    m = _REL_RE.search(t)
    if m and ("hace" in t or re.search(r"\bago\b", t) or re.fullmatch(r"\d+\s*\w+", t)):
        n = 1 if m.group("n") in {"un", "una"} else int(m.group("n"))
        delta = _relative_delta(m.group("u").lower(), n)
        if delta:
            return now - delta
    try:
        # dd/mm/yyyy is day-first, but ISO dates are year-month-day: dayfirst would read 2026-08-12 as 8 December.
        dt = dateparser.parse(t, dayfirst=not _ISO_DATE_RE.match(t))
    except (ValueError, OverflowError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_raw(raw: dict[str, Any], now: datetime | None = None, monthly_payments: int = 14) -> JobOffer:
    """Convert a portal adapter dict into a JobOffer.

    Expected keys: source, external_id, url, title; optional: company, location, salary_text,
    contract_type, description, skills, published_at (ISO) or published_text, work_mode_text.
    """
    now = now or datetime.now(timezone.utc)
    description = clean_text(raw.get("description"))
    if raw.get("experience_text"):
        description = f"{description}\nExperiencia mínima: {raw['experience_text']}".strip()
    title = clean_text(raw.get("title"))
    salary_min, salary_max = parse_salary(
        raw.get("salary_text") or _salary_from_text(description), monthly_payments
    )
    published = parse_date(raw.get("published_at"), now) or parse_date(raw.get("published_text"), now)
    work_mode = detect_work_mode(raw.get("work_mode_text"), raw.get("location"), title)
    if work_mode is WorkMode.UNKNOWN:
        work_mode = detect_work_mode(description[:1500])
    skills = [s.strip().lower() for s in raw.get("skills") or [] if s and s.strip()]
    return JobOffer(
        id=make_offer_id(raw["source"], str(raw["external_id"])),
        source=raw["source"],
        external_id=str(raw["external_id"]),
        url=raw["url"],
        title=title,
        company=clean_text(raw.get("company")),
        location=clean_text(raw.get("location")),
        work_mode=work_mode,
        salary_min=salary_min,
        salary_max=salary_max,
        contract_type=clean_text(raw.get("contract_type")),
        experience_years_min=parse_experience_years(f"{title}\n{description}"),
        description_md=description,
        skills=list(dict.fromkeys(skills)),
        published_at=published,
        scraped_at=now,
        raw={k: v for k, v in raw.items() if k != "description"},
    )


def _salary_from_text(description: str) -> str | None:
    m = re.search(r"(?:salario|sueldo|salary|retribuci[oó]n)[^\n]{0,60}", description, re.IGNORECASE)
    return m.group(0) if m else None
