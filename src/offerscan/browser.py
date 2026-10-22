"""The headless Chromium used by Scrapling's stealth session (InfoJobs, Indeed and Glassdoor when they block).

`scrapling install` is not enough on its own: it installs through `playwright` and then writes a marker file, so
once the marker exists it never reinstalls, even if the browser was deleted or a Patchright upgrade needs a newer
Chromium build. Here we install and check the exact executable Patchright will launch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

INSTALL_HINT = "offerscan install-browser"
MISSING_MARKERS = ("Executable doesn't exist", "playwright install", "patchright install")


def executable_path() -> Path | None:
    """Where Patchright expects Chromium for the installed version (None if Patchright itself is missing)."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path)
    except Exception:  # noqa: BLE001 - a broken driver means "not ready", never a crash in doctor
        return None


def is_installed() -> bool:
    path = executable_path()
    return path is not None and path.exists()


def install(with_deps: bool = False) -> int:
    """Download Chromium for Patchright; `with_deps` also installs system libraries (Linux, needs root)."""
    cmd = [sys.executable, "-m", "patchright", "install", "chromium"]
    if with_deps:
        cmd.append("--with-deps")
    return subprocess.call(cmd)


def launch_check() -> str | None:
    """Open and close a headless page; returns the error text, or None when the browser really works."""
    try:
        from patchright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<title>ok</title>")
            title = page.title()
            browser.close()
        return None if title == "ok" else f"respuesta inesperada: {title!r}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:200]}"


def is_missing_browser_error(message: str) -> bool:
    return any(marker in message for marker in MISSING_MARKERS)
