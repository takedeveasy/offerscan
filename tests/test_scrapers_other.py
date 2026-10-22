import json
from pathlib import Path

from scrapling.spiders import Request

from offerscan.pipeline.normalize import normalize_raw
from offerscan.scrapers import REGISTRY
from offerscan.scrapers.base import decode_html, looks_blocked
from offerscan.scrapers.infojobs import (
    InfoJobsSpider,
    parse_api_detail,
    parse_api_list,
    parse_web_listing,
)
from offerscan.scrapers.linkedin import LinkedInSpider, parse_detail, parse_listing
from offerscan.settings import SearchConfig, Settings, SourceConfig


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_linkedin_listing(fixtures_dir):
    items = parse_listing(decode_html((fixtures_dir / "linkedin" / "listing.html").read_bytes()))
    assert len(items) == 10
    first = items[0]
    assert first["external_id"] == "4478469035"
    assert first["title"] == "Jr AI Engineer (Workflows)"
    assert first["company"] == "X-ELIO"
    assert first["location"].startswith("Madrid")
    assert first["published_at"] == "2026-10-14"
    assert first["url"] == "https://www.linkedin.com/jobs/view/4478469035"
    assert all(i["title"] and i["company"] for i in items)


def test_linkedin_detail_and_normalize(fixtures_dir):
    listing = parse_listing(decode_html((fixtures_dir / "linkedin" / "listing.html").read_bytes()))
    detail = parse_detail(decode_html((fixtures_dir / "linkedin" / "detail.html").read_bytes()))
    assert len(detail["description"]) > 200
    assert detail["criteria"]
    offer = normalize_raw({**listing[0], **detail})
    assert offer.source == "linkedin" and offer.description_md


def test_infojobs_api_list_and_detail(fixtures_dir):
    items, pages = parse_api_list(load_json(fixtures_dir / "infojobs" / "api_list.synthetic.json"))
    assert pages == 1 and len(items) == 2
    junior, senior = items
    assert junior["company"] == "Acme Software S.L."
    assert junior["location"] == "Madrid, Madrid"
    assert junior["salary_text"] == "22.000 - 26.000 Bruto/año"
    detail = parse_api_detail(load_json(fixtures_dir / "infojobs" / "api_detail.synthetic.json"))
    assert "- Mentoring continuo" in detail["description"]
    assert detail["skills"] == ["Python", "SQL"]
    offer = normalize_raw({**junior, **detail})
    assert (offer.salary_min, offer.salary_max) == (22000, 26000)
    assert offer.experience_years_min == 1
    assert offer.work_mode.value == "hybrid"
    assert normalize_raw(senior).experience_years_min == 5
    assert normalize_raw(senior).salary_min == 45000


def test_infojobs_captcha_page_is_detected_not_parsed(fixtures_dir):
    html = decode_html((fixtures_dir / "infojobs" / "captcha_blocked.html").read_bytes())
    assert looks_blocked(html)
    assert parse_web_listing(html) == []


def test_registry_and_start_requests(tmp_path):
    assert {"tecnoempleo", "infojobs", "linkedin"} <= set(REGISTRY)
    cfg = SearchConfig(keywords=["python", "react"], locations=["Madrid"])
    settings = Settings(infojobs_client_id="id", infojobs_client_secret="secret")
    spider = InfoJobsSpider(cfg, SourceConfig(), settings=settings, data_dir=tmp_path)
    reqs = spider.build_start_requests()
    assert len(reqs) == 2 and all(isinstance(r, Request) for r in reqs)
    assert "api.infojobs.net/api/9/offer" in reqs[0].url
    assert reqs[0]._session_kwargs["headers"]["Authorization"].startswith("Basic ")
    assert "q=python&" in reqs[0].url and "province=madrid" in reqs[0].url

    canarias = InfoJobsSpider(SearchConfig(keywords=["java"], locations=["Las Palmas", "A Coruña", "Remoto"]),
                              SourceConfig(), settings=settings, data_dir=tmp_path)  # fmt: skip
    urls = [r.url for r in canarias.build_start_requests()]
    assert any("province=las-palmas" in u for u in urls) and any("province=a-coruna" in u for u in urls)

    tecno = REGISTRY["tecnoempleo"](cfg, SourceConfig(), data_dir=tmp_path)
    assert "ex=,1,2,3,4," in tecno.build_start_requests()[0].url
    li = LinkedInSpider(cfg, SourceConfig(), data_dir=tmp_path)
    assert "f_E=1%2C2" in li.build_start_requests()[0].url


def test_infojobs_web_listing_from_real_stealth_page(fixtures_dir):
    html = decode_html((fixtures_dir / "infojobs" / "listing_stealth.html").read_bytes())
    assert not looks_blocked(html)
    items = parse_web_listing(html)
    assert len(items) == 5
    first = items[0]
    assert first["external_id"].startswith("of-i") and first["url"].startswith("https://www.infojobs.net/")
    assert first["title"] and first["company"] and first["location"]
    assert any(i["salary_text"] and "\u20ac" in i["salary_text"] for i in items)
    offer = normalize_raw(first)
    assert offer.published_at is not None
    assert normalize_raw(next(i for i in items if i["salary_text"] and "\u20ac" in i["salary_text"])).salary_min
