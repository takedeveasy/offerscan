from datetime import datetime, timedelta, timezone

import pytest

from offerscan.models import WorkMode
from offerscan.pipeline.normalize import (
    clean_text,
    detect_work_mode,
    normalize_raw,
    parse_date,
    parse_experience_years,
    parse_salary,
)

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("25.000 - 30.000 €", (25000, 30000)),
        ("33.000&euro; - 39.000&euro; b/a", (33000, 39000)),
        ("33.000€ - 39.000€ b/a", (33000, 39000)),
        ("25k-30k", (25000, 30000)),
        ("25K", (25000, 25000)),
        ("1.800€/mes", (25200, 25200)),
        ("1800 euros al mes", (25200, 25200)),
        ("Entre 24.000 y 28.000 euros brutos anuales", (24000, 28000)),
        ("30000€", (30000, 30000)),
        ("12 €/hora", (None, None)),
        ("A convenir", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
        ("Contrato de 12 meses", (None, None)),
        ("500 €", (None, None)),
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_parse_salary_monthly_payments_configurable():
    assert parse_salary("2.000€/mes", monthly_payments=12) == (24000, 24000)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Al menos 2 años de experiencia en Python", 2),
        ("3+ years of experience with Java", 3),
        ("Experiencia mínima: 1 año", 1),
        ("Se requieren 5 años de experiencia", 5),
        ("más de 3 años de experiencia", 3),
        ("Entre 2 y 3 años de experiencia", 2),
        ("2 años de experiencia en Python y 1 año en Docker", 2),
        ("Sin experiencia previa, formamos a tu medida", 0),
        ("No se requiere experiencia", 0),
        ("Grado de 4 años en informática", None),
        ("Contrato de 2 años", None),
        ("Menos de 1 año de experiencia", 0),
        ("Buscamos un perfil con dos años de experiencia", 2),
        ("", None),
        (None, None),
    ],
)
def test_parse_experience_years(text, expected):
    assert parse_experience_years(text) == expected


@pytest.mark.parametrize(
    "texts,expected",
    [
        (("100% remoto",), WorkMode.REMOTE),
        (("Teletrabajo parcial híbrido",), WorkMode.HYBRID),
        (("Barcelona (Híbrido)",), WorkMode.HYBRID),
        (("Fully remote",), WorkMode.REMOTE),
        (("Presencial en Madrid",), WorkMode.ONSITE),
        (("Madrid", None), WorkMode.UNKNOWN),
    ],
)
def test_detect_work_mode(texts, expected):
    assert detect_work_mode(*texts) is expected


@pytest.mark.parametrize(
    "text,delta",
    [
        ("hace 3 días", timedelta(days=3)),
        ("hace 2 horas", timedelta(hours=2)),
        ("hace 1 semana", timedelta(weeks=1)),
        ("hace un mes", timedelta(days=30)),
        ("6 days ago", timedelta(days=6)),
        ("2 weeks ago", timedelta(weeks=2)),
        ("hace 15 minutos", timedelta(minutes=15)),
        ("ayer", timedelta(days=1)),
        ("hoy", timedelta(0)),
    ],
)
def test_parse_relative_date(text, delta):
    assert parse_date(text, NOW) == NOW - delta


def test_parse_absolute_dates():
    assert parse_date("21/10/2026", NOW).date().isoformat() == "2026-10-21"
    assert parse_date("2026-10-14", NOW).date().isoformat() == "2026-10-14"
    assert parse_date("2026-08-12T05:00:00+00:00", NOW).date().isoformat() == "2026-08-12"
    assert parse_date("2026-10-07T00:58:27Z", NOW).date().isoformat() == "2026-10-07"
    assert parse_date("07/10/2026", NOW).date().isoformat() == "2026-10-07"
    assert parse_date("basura", NOW) is None
    assert parse_date(None, NOW) is None


def test_clean_text():
    assert clean_text("  Hola&nbsp;&amp;\xa0mundo \n\n\n\n adios ") == "Hola & mundo\n\nadios"


def test_normalize_raw_full():
    raw = {
        "source": "tecnoempleo",
        "external_id": "abc",
        "url": "https://x/1",
        "title": "Junior Python Developer",
        "company": "Acme ",
        "location": "Madrid",
        "salary_text": "24.000€ - 28.000€ b/a",
        "description": "Buscamos junior. Al menos 1 año de experiencia. Trabajo híbrido.",
        "skills": ["Python", "python", "Docker"],
        "published_at": "2026-10-20",
    }
    offer = normalize_raw(raw, NOW)
    assert offer.salary_min == 24000 and offer.salary_max == 28000
    assert offer.experience_years_min == 1
    assert offer.work_mode is WorkMode.HYBRID
    assert offer.skills == ["python", "docker"]
    assert offer.published_at.date().isoformat() == "2026-10-20"
    assert offer.id == normalize_raw(raw, NOW).id
