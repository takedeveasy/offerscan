"""Live smoke tests. Run with: pytest --live"""

import pytest
from scrapling.fetchers import Fetcher

from offerscan.scrapers.base import decode_html
from offerscan.scrapers.linkedin import parse_listing as li_listing
from offerscan.scrapers.tecnoempleo import parse_listing as te_listing

pytestmark = pytest.mark.live


def test_tecnoempleo_live():
    r = Fetcher.get("https://www.tecnoempleo.com/ofertas-trabajo/?te=python", impersonate="chrome")
    items, _ = te_listing(decode_html(r.body))
    assert len(items) > 5


def test_linkedin_live():
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        "?keywords=python%20junior&location=Spain&f_E=1%2C2&start=0"
    )
    r = Fetcher.get(url, impersonate="chrome")
    assert li_listing(decode_html(r.body))


def test_infojobs_api_live(monkeypatch):
    from offerscan.settings import Settings

    s = Settings()
    if not (s.infojobs_client_id and s.infojobs_client_secret):
        pytest.skip("INFOJOBS credentials not configured")
    from offerscan.scrapers.infojobs import InfoJobsSpider
    from offerscan.settings import SearchConfig

    spider = InfoJobsSpider(SearchConfig(keywords=["python"]), settings=s)
    req = spider.build_start_requests()[0]
    r = Fetcher.get(req.url, headers=req._session_kwargs["headers"])
    assert r.status == 200
