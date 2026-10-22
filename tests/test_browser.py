import asyncio
from pathlib import Path

from typer.testing import CliRunner

from offerscan import browser
from offerscan.cli import app
from offerscan.scrapers import runner
from offerscan.settings import SearchConfig, Settings


def fake_browser(monkeypatch, tmp_path: Path, *, exists: bool, launch_error: str | None = None, rc: int = 0):
    exe = tmp_path / "chrome.exe"
    if exists:
        exe.write_text("")
    calls: list[bool] = []
    monkeypatch.setattr(browser, "executable_path", lambda: exe)
    monkeypatch.setattr(browser, "launch_check", lambda: launch_error)
    monkeypatch.setattr(browser, "install", lambda with_deps=False: calls.append(with_deps) or rc)
    return calls


def test_install_browser_downloads_and_verifies(monkeypatch, tmp_path):
    calls = fake_browser(monkeypatch, tmp_path, exists=True)
    result = CliRunner().invoke(app, ["install-browser", "--with-deps"])
    assert result.exit_code == 0, result.output
    assert calls == [True]
    assert "Navegador listo" in result.output


def test_install_browser_check_only_reports_missing(monkeypatch, tmp_path):
    calls = fake_browser(monkeypatch, tmp_path, exists=False)
    result = CliRunner().invoke(app, ["install-browser", "--check"])
    assert result.exit_code == 1
    assert calls == []
    assert "offerscan install-browser" in result.output


def test_install_browser_fails_when_chromium_does_not_start(monkeypatch, tmp_path):
    fake_browser(monkeypatch, tmp_path, exists=True, launch_error="Error: libnss3.so missing")
    result = CliRunner().invoke(app, ["install-browser"])
    assert result.exit_code == 1
    assert "no arranca" in result.output


def test_install_browser_fails_when_download_fails(monkeypatch, tmp_path):
    fake_browser(monkeypatch, tmp_path, exists=False, rc=1)
    assert CliRunner().invoke(app, ["install-browser"]).exit_code == 1


def test_doctor_points_to_install_browser(monkeypatch):
    monkeypatch.setattr(browser, "is_installed", lambda: False)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "install-browser" in result.output.replace("\n", " ").replace("|", " ").replace(" ", "")


def test_missing_browser_error_detection():
    assert browser.is_missing_browser_error("BrowserType.launch: Executable doesn't exist at C:\\chrome.exe")
    assert not browser.is_missing_browser_error("TimeoutError: page took too long")


class _Stats:
    requests_count, failed_requests_count, blocked_requests_count = 2, 0, 2


class _BlockedSpider:
    """Stands in for a stealth-capable portal whose pages were all blocked."""

    stealth_fallback = True

    def __init__(self, *args, **kwargs):
        self.final_stats = _Stats()

    async def stream(self):
        return
        yield


def test_runner_explains_a_missing_browser(monkeypatch, tmp_path):
    monkeypatch.setitem(runner.REGISTRY, "fake", _BlockedSpider)
    monkeypatch.setattr(browser, "is_installed", lambda: False)
    settings = Settings(data_dir=tmp_path)
    result, items = asyncio.run(
        runner._crawl_source("fake", SearchConfig(), settings, max_pages=1, dev=False, on_item=None)
    )
    assert items == [] and result.blocked == 2
    assert "offerscan install-browser" in result.error

    monkeypatch.setattr(browser, "is_installed", lambda: True)
    result, _ = asyncio.run(runner._crawl_source("fake", SearchConfig(), settings, max_pages=1, dev=False, on_item=None))
    assert result.error == ""
