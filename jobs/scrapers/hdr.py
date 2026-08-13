"""Scraper for HDR's Taleo careers board (https://hdr.taleo.net).

HDR is an architecture/engineering firm on Oracle Taleo, whose job list is
rendered entirely client-side -- no HTTP client sees the listings -- so this is
one of the few sources that needs a headless browser (see ``_browser``). HDR
skews engineering, so ``require_keep=True`` + the seniority screen keep only the
entry-level design roles (Designer, BIM Designer, intern), and the pipeline
location gate keeps only target markets.

Each rendered row is a table row: title (a ``jobdetail.ftl?job=`` link),
location, and posting date. Only the first results page is read (Taleo paginates
via JS; page one is the most recent postings). Feeds the ``architecture`` track.

Independent and importable: ``from scrapers import hdr`` then ``hdr.scrape()``.
Errors raise to the caller (the pipeline isolates them; if Playwright isn't
installed the whole scraper is skipped).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import _browser, _employment, _filter

log = logging.getLogger("scrapers.hdr")

BASE_URL = "https://hdr.taleo.net"
SEARCH_URL = f"{BASE_URL}/careersection/ex/jobsearch.ftl"
JOB_LINK_SELECTOR = "a[href*='jobdetail.ftl?job=']"
_DATE_FMT = "%b %d, %Y"  # "Aug 13, 2026"

ARCH_SOFTWARE_TAGS: dict[str, tuple[str, ...]] = {
    "Revit": ("revit",),
    "Rhino": ("rhino", "rhinoceros"),
    "AutoCAD": ("autocad", "auto cad"),
    "BIM": ("bim",),
}


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _parse_date(text: str) -> str:
    try:
        return datetime.strptime(_clean(text), _DATE_FMT).date().isoformat()
    except ValueError:
        return _today_iso()


def _software_tags(text: str) -> list[str]:
    low = text.lower()
    return [tag for tag, pats in ARCH_SOFTWARE_TAGS.items() if any(p in low for p in pats)]


def _parse_row(anchor) -> dict | None:
    title = _clean(anchor.get_text())
    if not title:
        return None
    if not _filter.is_undergrad_accessible(title, require_keep=True, drop_seniority=True):
        return None

    title_cell = anchor.find_parent(["td", "th"])
    loc_cell = title_cell.find_next_sibling(["td", "th"]) if title_cell else None
    date_cell = loc_cell.find_next_sibling(["td", "th"]) if loc_cell else None
    location = _clean(loc_cell.get_text(" ")) if loc_cell else ""
    posted = _parse_date(date_cell.get_text()) if date_cell else _today_iso()

    emp = _employment.classify(title, default="Full-Time")
    tags = [emp] if emp else []
    for t in _software_tags(title):
        if t not in tags:
            tags.append(t)

    return {
        "id": _make_id(title, "HDR"),
        "title": title,
        "company": "HDR",
        "location": location,
        "url": urljoin(BASE_URL, anchor["href"]),
        "posted_date": posted,
        "scraped_date": _today_iso(),
        "active": True,
        "tags": tags,
    }


def scrape() -> list[dict]:
    """Scrape HDR's Taleo board (first page). Returns a list of listing dicts."""
    html = _browser.render(SEARCH_URL, wait_selector=JOB_LINK_SELECTOR, settle_ms=3000)
    soup = BeautifulSoup(html, "html.parser")

    by_id: dict[str, dict] = {}
    for anchor in soup.select(JOB_LINK_SELECTOR):
        item = _parse_row(anchor)
        if item is not None:
            by_id.setdefault(item["id"], item)

    listings = list(by_id.values())
    log.info("HDR: %d undergrad-accessible listing(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
