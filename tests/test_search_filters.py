import json

from offerscan.geo import PROVINCES, find_province, matches_location, split_locations
from offerscan.scrapers.base import decode_html
from offerscan.scrapers.infojobs import keyword_slug, parse_web_detail, parse_web_listing, web_search_url
from offerscan.scrapers.runner import post_filter
from offerscan.scrapers.tecnoempleo import experience_filter, parse_listing, search_url
from offerscan.settings import SearchConfig


def test_province_table_matches_real_tecnoempleo_form(fixtures_dir):
    from scrapling.parser import Selector

    html = decode_html((fixtures_dir / "tecnoempleo" / "search_form.html").read_bytes())
    options = {o.attrib["value"] for o in Selector(html).css("select#pr option") if o.attrib.get("value")}
    assert {str(p.tecnoempleo_id) for p in PROVINCES} == options
    names = {o.attrib["value"]: o.get_all_text().strip() for o in Selector(html).css("select#pr option")}
    assert names["263"] == "Madrid" and names["240"] == "Barcelona"


def test_province_slugs_exist_on_real_infojobs(fixtures_dir):
    slugs = set(json.loads((fixtures_dir / "infojobs" / "province_slugs.json").read_text()))
    assert {p.infojobs_slug for p in PROVINCES} <= slugs


def test_find_province_aliases():
    assert find_province("madrid").tecnoempleo_id == 263
    assert find_province("Vizcaya").name == "Bizkaia"
    assert find_province("a coruña").infojobs_slug == "a-coruna"
    assert find_province("Narnia") is None
    provinces, remote, unknown = split_locations(["Madrid", "Remoto", "Narnia"])
    assert [p.name for p in provinces] == ["Madrid"] and remote and unknown == ["Narnia"]


def test_tecnoempleo_search_url():
    madrid, barna = find_province("Madrid"), find_province("Barcelona")
    assert experience_filter(0) == ",1,2," and experience_filter(1) == ",1,2,3," and experience_filter(2) == ",1,2,3,4,"
    url = search_url("c#", [madrid, barna], ["hybrid", "remote"], 1)
    assert url == "https://www.tecnoempleo.com/ofertas-trabajo/?te=c%23&ex=,1,2,3,&pr=,263,240,&en_remoto=,1,3,"
    assert "pr=" not in search_url("react", [], [], 2)


def test_tecnoempleo_province_fixture_has_madrid_offers(fixtures_dir):
    html = decode_html((fixtures_dir / "tecnoempleo" / "listing_react_madrid.html").read_bytes())
    items, _ = parse_listing(html)
    assert items and any("Madrid" in i["location"] for i in items)
    remote = decode_html((fixtures_dir / "tecnoempleo" / "listing_react_remote.html").read_bytes())
    assert all("remoto" in i["work_mode_text"].lower() for i in parse_listing(remote)[0])


def test_infojobs_search_url_uses_category_and_province_path():
    assert keyword_slug(".NET") == "net" and keyword_slug("C#") == "c%23" and keyword_slug("Machine Learning") == "machine-learning"
    assert web_search_url("react", "madrid") == (
        "https://www.infojobs.net/ofertas-trabajo/madrid/informatica-telecomunicaciones/react"
    )
    assert web_search_url("react", "", 2).endswith("/informatica-telecomunicaciones/react?page=2")


def test_infojobs_real_page_state_confirms_filters(fixtures_dir):
    state = (fixtures_dir / "infojobs" / "search_state_madrid_it.txt").read_text()
    assert '"provinceIds":["33"]' in state and '"categoryIds":["150"]' in state


def test_infojobs_listing_and_detail_fixtures(fixtures_dir):
    items = parse_web_listing(decode_html((fixtures_dir / "infojobs" / "listing_react_madrid_it.html").read_bytes()))
    melt = next(i for i in items if i["title"].startswith("Frontend Developer (React)"))
    assert melt["company"] and melt["url"].startswith("https://www.infojobs.net/madrid/")
    detail = parse_web_detail(decode_html((fixtures_dir / "infojobs" / "detail_melt.html").read_bytes()))
    assert len(detail["description"]) > 1000 and "React" in detail["description"]
    assert "5" in detail["experience_text"]


def test_post_filter_applies_what_portal_cannot():
    cfg = SearchConfig(locations=["Madrid"], work_modes=["hybrid"])
    items = [
        {"title": "a", "location": "Sevilla", "work_mode_text": "Sevilla Hibrido"},
        {"title": "b", "location": "Madrid", "work_mode_text": "Madrid Hibrido"},
        {"title": "c", "location": "Madrid", "work_mode_text": "Madrid Solo teletrabajo"},
        {"title": "d", "location": "Madrid", "work_mode_text": ""},
    ]
    assert [i["title"] for i in post_filter("linkedin", items, cfg)] == ["b", "d"]
    assert [i["title"] for i in post_filter("infojobs", items, cfg)] == ["a", "b", "d"]  # province is server-side
    assert post_filter("tecnoempleo", items, cfg) == items


def test_detail_budget_skips_prefiltered_titles_and_scales_with_search():
    from offerscan.scrapers.tecnoempleo import TecnoempleoSpider
    from offerscan.service import SearchFilters, apply_search_filters
    from offerscan.settings import Settings, SourceConfig

    spider = TecnoempleoSpider(SearchConfig(), SourceConfig(max_details=5), settings=Settings())
    assert not spider.want_detail({"title": "Senior Java Developer", "description": ""})
    assert not spider.want_detail({"title": "Analista funcional SAP", "description": ""})
    assert spider.details_requested == 0
    assert spider.want_detail({"title": "Junior Python Developer", "description": ""})
    assert spider.details_requested == 1

    cfg = apply_search_filters(
        SearchConfig(sources={"tecnoempleo": SourceConfig(max_details=30)}),
        SearchFilters(keywords=["python", "java", "react"], sources=["tecnoempleo"], max_pages=2),
    )
    assert cfg.sources["tecnoempleo"].max_details == 100 and cfg.sources["tecnoempleo"].max_pages == 2


def test_matches_location_remote_and_aliases():
    assert matches_location("Bilbao, Vizcaya", "onsite", ["Bizkaia"])
    assert matches_location("Sevilla", "remote", ["Madrid", "Remoto"])
    assert not matches_location("Sevilla", "onsite", ["Madrid", "Remoto"])
