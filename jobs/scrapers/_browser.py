"""Headless-browser page fetcher for JS-gated boards.

A few boards can't be reached with an HTTP client at all: they render listings
client-side (Taleo) or gate the page behind a JavaScript-set cookie / bot
challenge (iCIMS, AIA). For those -- and only those -- this renders the page with
Playwright's headless Chromium and returns the finished HTML for BeautifulSoup.

Playwright is imported lazily so importing this module never fails; a caller that
actually renders without Playwright installed gets a clear error, which
``pipeline.run_scrapers`` isolates (that scraper is skipped, the run continues).
In CI the workflow runs ``playwright install --with-deps chromium`` first.

Prefer ``requests`` / ``_http`` whenever they work -- this is the heavyweight
last resort.
"""

from __future__ import annotations

import logging

log = logging.getLogger("scrapers.browser")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_TIMEOUT_MS = 45000


def render(
    url: str,
    *,
    wait_selector: str | None = None,
    wait_until: str = "networkidle",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    settle_ms: int = 0,
) -> str:
    """Load ``url`` in headless Chromium and return the rendered HTML.

    ``wait_selector`` waits for a specific element (the job list) before reading;
    ``settle_ms`` adds a fixed pause for late XHRs.
    """
    from playwright.sync_api import sync_playwright  # lazy: keeps import cost off

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = browser.new_context(
                user_agent=USER_AGENT, viewport={"width": 1280, "height": 900}
            )
            page = context.new_page()
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=timeout_ms)
                except Exception:  # noqa: BLE001 - return whatever rendered
                    log.warning("render: selector %r not found on %s", wait_selector, url)
            if settle_ms:
                page.wait_for_timeout(settle_ms)
            return page.content()
        finally:
            browser.close()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    html = render(sys.argv[1] if len(sys.argv) > 1 else "https://example.com")
    print(f"rendered {len(html)} chars")
