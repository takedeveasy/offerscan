"""Parsers and start requests of the extra portals, against real pages saved on 2026-10-21."""

import json
from pathlib import Path

import pytest

from offerscan.pipeline.normalize import normalize_raw
from offerscan.scrapers import (
    glassdoor,
    himalayas,
    indeed,
    infoempleo,
    jobfluent,
    jooble,
    manfred,
    remotive,
    talent,
    trabajos,
)
from offerscan.scrapers.base import REGISTRY, decode_html, job_posting_ld, ld_detail, mentions_keyword
from offerscan.scrapers.runner import SERVER_SIDE_FILTERS
from offerscan.settings import PrefilterConfig, SearchConfig, SourceConfig

EXTRA = ["indeed", "glassdoor", "infoempleo", "trabajos", "jobfluent", "manfred", "talent", "jooble", "himalayas",
         "remotive"]  # fmt: skip


def html(fixtures_dir: Path, rel: str) -> str:
    return decode_html((fixtures_dir / rel).read_bytes())


def load_json(fixtures_dir: Path, rel: str):
    return json.loads((fixtures_dir / rel).read_text(encoding="utf-8"))


def assert_normalizes(items: list[dict]) -> None:
    for item in items:
        offer = normalize_raw(item)
        assert offer.title and offer.url.startswith("https://")


def test_every_extra_portal_is_registered_and_has_filter_info():
    assert set(EXTRA) <= set(REGISTRY)
    assert set(REGISTRY) <= set(SERVER_SIDE_FILTERS)


def test_indeed_listing_from_embedded_json(fixtures_dir):
    items = indeed.parse_listing(html(fixtures_dir, "indeed/listing.html"))
    assert len(items) == 15
    first = items[0]
    assert first["external_id"] == "d179c50665a20e88"
    assert first["url"] == "https://es.indeed.com/viewjob?jk=d179c50665a20e88"
    assert first["title"] == "Analista de Datos Junior / Python"
    assert first["company"] == "TECDATA ENGINEERING"
    assert first["published_at"].startswith("2026-07-27")
    offer = normalize_raw(items[1])
    assert (offer.salary_min, offer.salary_max) == (27000, 38000)
    assert offer.work_mode.value == "remote"
    assert_normalizes(items)


def test_indeed_search_urls():
    assert indeed.search_url("python", "Madrid") == "https://es.indeed.com/jobs?q=python&l=Madrid"
    assert "sc=0kf%3Aattr%28DSQF7%29%3B" in indeed.search_url("python", remote=True)
    spider = indeed.IndeedSpider(SearchConfig(keywords=["java"], locations=["Remoto"]), SourceConfig())
    assert "DSQF7" in spider.build_start_requests()[0].url


def test_glassdoor_listing_and_url(fixtures_dir):
    items = glassdoor.parse_listing(html(fixtures_dir, "glassdoor/listing.html"))
    assert len(items) == 30
    assert items[0]["external_id"] == "1010085434372"
    assert items[0]["company"] == "Airbus" and items[0]["location"] == "Getafe"
    assert all(i["description"] for i in items)
    assert_normalizes(items)
    assert glassdoor.search_url("python").endswith("/Empleo/espana-python-empleos-SRCH_IL.0,6_IN219_KO7,13.htm")
    assert glassdoor.search_url("C#").endswith("espana-c-empleos-SRCH_IL.0,6_IN219_KO7,8.htm")


def test_infoempleo_listing_detail_and_urls(fixtures_dir):
    items = infoempleo.parse_listing(html(fixtures_dir, "infoempleo/listing.html"))
    assert len(items) == 21
    first = items[0]
    assert first["external_id"] == "3202113"
    assert first["title"] == "Backend developer (H/M/X) - ( PYTHON - JAVA- C#)"
    assert first["experience_text"] == "No se requiere experiencia"
    assert normalize_raw(first).experience_years_min == 0
    assert_normalizes(items)
    assert len(infoempleo.parse_listing(html(fixtures_dir, "infoempleo/listing_primer_empleo.html"))) == 10

    detail = ld_detail(html(fixtures_dir, "infoempleo/detail.html"))
    assert len(detail["description"]) > 300 and detail["published_at"] == "2026-09-25"

    from offerscan.geo import find_province

    coruna = find_province("A Coruña")
    assert infoempleo.search_url("python", coruna) == "https://www.infoempleo.com/trabajo/en_a-corunna/?search=python"
    assert infoempleo.search_url("python", page=2, first_job=True).endswith("/primer-empleo/?search=python&pagina=2")
    cfg = SearchConfig(keywords=["python"], prefilter=PrefilterConfig(max_experience_years=0))
    assert "/primer-empleo/" in infoempleo.InfoempleoSpider(cfg, SourceConfig()).build_start_requests()[0].url


def test_trabajos_listing_detail_and_urls(fixtures_dir):
    items = trabajos.parse_listing(html(fixtures_dir, "trabajos/listing.html"))
    assert len(items) == 24
    assert items[0]["external_id"] == "1196551163" and items[0]["company"] == "AdegiLan"
    assert items[1]["salary_text"] == "18.000 € - 30.000 €" and "€" not in items[1]["contract_type"]
    assert normalize_raw(items[0]).published_at.date().isoformat() == "2026-10-21"
    assert_normalizes(items)

    detail = trabajos.parse_detail(html(fixtures_dir, "trabajos/detail.html"))
    assert "Business Intelligence" in detail["description"]
    assert detail["experience_text"] == "Sin Experiencia"
    assert "salary_text" not in detail  # "A convenir"
    assert trabajos.search_url("Node JS", "a-coruna") == "https://www.trabajos.com/ofertas-empleo/a-coruna/node-js"


def test_jobfluent_listing_detail_and_cities(fixtures_dir):
    items, next_url = jobfluent.parse_listing(html(fixtures_dir, "jobfluent/listing.html"))
    assert len(items) == 25
    assert items[0]["title"] == "Software Engineer I, Backend (Collections)" and items[0]["company"] == "Affirm"
    assert items[0]["skills"] == ["Kotlin", "Python"]
    assert next_url.endswith("/es/empleos-madrid?page=2&q=python")

    detail = jobfluent.parse_detail(html(fixtures_dir, "jobfluent/detail.html"))
    assert detail["location"] == "Barcelona" and detail["published_at"] == "2026-10-09"
    assert len(detail["description"]) > 300

    assert jobfluent.cities_for([]) == ["madrid", "barcelona", "valencia"]
    assert jobfluent.cities_for(["Valencia", "Sevilla", "Remoto"]) == ["valencia", "remoto"]
    assert jobfluent.cities_for(["Sevilla"]) == []


def test_manfred_offers_and_keyword_filter(fixtures_dir):
    items = manfred.parse_offers(load_json(fixtures_dir, "manfred/offers.json"))
    assert len(items) == 8
    first = items[0]
    assert first["url"].startswith("https://www.getmanfred.com/ofertas-empleo/8511/")
    assert first["work_mode_text"] == "hibrido" and first["salary_text"] == "45000€ b/a"
    assert_normalizes(items)
    detail = manfred.parse_detail(html(fixtures_dir, "manfred/detail.html"))
    assert detail["skills"][:3] == ["NodeJS", "Express", "React Native"]
    assert "Imprescindible: NodeJS, Express, React Native" in detail["description"]
    assert "React Native" in detail["description"].split("Responsabilidades:")[1]
    assert "work_mode_text" not in detail
    assert manfred.work_mode_text(100) == "remoto" and manfred.work_mode_text(0) == "presencial"


def test_talent_listing_and_detail(fixtures_dir):
    items = talent.parse_listing(html(fixtures_dir, "talent/listing.html"))
    assert len(items) == 14
    assert items[0]["url"] == "https://es.talent.com/view?id=535270591515594461"
    assert items[0]["company"] == "SlashMobility" and items[0]["location"] == "Madrid, Madrid"
    assert "Mostrar" not in items[0]["description"]
    assert_normalizes(items)
    assert ld_detail(html(fixtures_dir, "talent/detail.html"))["contract_type"] == "Full Time"


def test_jooble_listing_skips_ad_widgets(fixtures_dir):
    items = jooble.parse_listing(html(fixtures_dir, "jooble/listing.html"))
    assert len(items) == 20
    assert items[0]["url"] == "https://es.jooble.org/away/-2023793095849949974"
    assert len(items[0]["description"]) > 1000
    assert_normalizes(items)
    assert jooble.search_url("Node JS", "A Coruña") == "https://es.jooble.org/trabajo-node-js/A%20Coru%C3%B1a"


def test_himalayas_and_remotive_apis(fixtures_dir):
    him = himalayas.parse_jobs(load_json(fixtures_dir, "himalayas/search.json"))
    assert len(him) == 8 and all(i["work_mode_text"] == "remote" for i in him)
    assert him[1]["location"] == "Remoto (Spain)" and him[1]["experience_text"] == "Entry-level"
    assert "seniority=Entry-level" in himalayas.search_url("python")
    assert_normalizes(him)

    rem = remotive.parse_jobs(load_json(fixtures_dir, "remotive/jobs.json"))
    assert [i["external_id"] for i in rem] == ["2091158", "1680495"]  # the USA/Canada-only job is dropped
    assert_normalizes(rem)


@pytest.mark.parametrize(
    ("place", "ok"),
    [("Worldwide", True), ("Europe, USA", True), ("EMEA", True), ("USA only", False), ("LATAM", False), ("", True)],
)
def test_remotive_location_eligibility(place, ok):
    assert remotive.open_to_spain(place) is ok


def test_shared_helpers():
    assert mentions_keyword("Desarrollador Node.js y React", ["react"])
    assert mentions_keyword("Senior C# developer", ["C#"])
    assert not mentions_keyword("Reactor nuclear", ["react"])
    assert not mentions_keyword("JavaScript developer", ["java"])
    graph = '<script type="application/ld+json">{"@graph": [{"@type": "Organization"}, {"@type": "JobPosting", "title": "x"}]}</script>'
    assert job_posting_ld(graph)["title"] == "x"


@pytest.mark.parametrize("name", EXTRA)
def test_start_requests_cover_keywords(name):
    cfg = SearchConfig(keywords=["python", "react"], locations=["Madrid"])
    reqs = REGISTRY[name](cfg, SourceConfig()).build_start_requests()
    assert reqs and all(r.url.startswith("https://") for r in reqs)
