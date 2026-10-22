from offerscan.pipeline.normalize import normalize_raw
from offerscan.scrapers.base import decode_html, html_to_text, looks_blocked
from offerscan.scrapers.tecnoempleo import parse_detail, parse_listing


def read(fixtures_dir, name):
    return decode_html((fixtures_dir / "tecnoempleo" / name).read_bytes())


def test_listing_parses_cards(fixtures_dir):
    items, next_url = parse_listing(read(fixtures_dir, "listing.html"))
    assert len(items) == 30
    first = items[0]
    assert first["title"] == "Desarrollador/a Python sobre GCP"
    assert first["company"] == "Arelance"
    assert first["external_id"] == "rf-478e15e942b7f396ba44"
    assert first["published_at"] == "21/10/2026"
    assert first["salary_text"].startswith("33.000")
    assert "python" in first["skills"]
    assert all(i["url"].startswith("https://www.tecnoempleo.com/") and i["title"] for i in items)
    assert len({i["external_id"] for i in items}) == 30
    assert next_url == "https://www.tecnoempleo.com/ofertas-trabajo/?te=python&pagina=2"


def test_listing_hybrid_location(fixtures_dir):
    items, _ = parse_listing(read(fixtures_dir, "listing.html"))
    hybrid = [i for i in items if "brido" in i["work_mode_text"]]
    assert hybrid and hybrid[0]["location"]


def test_detail_parses_ld_json_and_description(fixtures_dir):
    detail = parse_detail(read(fixtures_dir, "detail.html"))
    assert "al menos 3 años de experiencia" in detail["description"]
    assert detail["published_at"] == "2026-10-21"
    assert detail["salary_text"] == "33000€ - 39000€ b/a"
    assert "python" in detail["skills"]


def test_listing_and_detail_normalize(fixtures_dir):
    items, _ = parse_listing(read(fixtures_dir, "listing.html"))
    raw = {**items[0], **parse_detail(read(fixtures_dir, "detail.html"))}
    offer = normalize_raw(raw)
    assert offer.salary_min == 33000 and offer.salary_max == 39000
    assert offer.experience_years_min == 3
    assert offer.work_mode.value == "remote"


def test_not_flagged_as_blocked(fixtures_dir):
    assert not looks_blocked(read(fixtures_dir, "listing.html"))
    assert not looks_blocked(read(fixtures_dir, "detail.html"))


def test_html_to_text():
    assert html_to_text("<p>Hola<br/>mundo</p><ul><li>a</li><li>b &amp; c</li></ul>") == (
        "Hola\nmundo\n\n- a\n- b & c"
    )


PORTAL_WIDE = {"autocad", "cobol", "marketing", "itil", "ansible", "redis"}


def test_detail_skills_are_offer_specific(fixtures_dir):
    skills = parse_detail(read(fixtures_dir, "detail.html"))["skills"]
    assert skills == ["python", "gcp", "cloud", "git"]
    assert not PORTAL_WIDE & set(skills)


def test_listing_skills_are_card_scoped_and_distinct(fixtures_dir):
    items, _ = parse_listing(read(fixtures_dir, "listing.html"))
    assert all(len(i["skills"]) <= 10 for i in items)
    assert all(not PORTAL_WIDE & set(i["skills"]) for i in items)
    long_lists = [tuple(i["skills"]) for i in items if len(i["skills"]) >= 4]
    assert len(long_lists) == len(set(long_lists)) or len(long_lists) <= 1
    assert len({tuple(i["skills"]) for i in items}) > 5


def test_other_portals_do_not_leak_skills(fixtures_dir):
    from offerscan.scrapers.infojobs import parse_web_listing
    from offerscan.scrapers.linkedin import parse_detail as li_detail
    from offerscan.scrapers.linkedin import parse_listing as li_listing

    assert all("skills" not in i for i in li_listing(read_any(fixtures_dir, "linkedin", "listing.html")))
    assert "skills" not in li_detail(read_any(fixtures_dir, "linkedin", "detail.html"))
    assert all("skills" not in i for i in parse_web_listing(read_any(fixtures_dir, "infojobs", "listing_stealth.html")))


def read_any(fixtures_dir, portal, name):
    return decode_html((fixtures_dir / portal / name).read_bytes())
