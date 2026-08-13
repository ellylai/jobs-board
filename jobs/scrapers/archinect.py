"""Scraper for Archinect jobs (https://archinect.com/jobs).

Filters for internship / co-op postings and extracts title, company, location,
url, and posted_date. Adds tags for any design software mentioned in the
listing title.

Independent and importable: ``from scrapers import archinect`` then
``archinect.scrape()``. Network/parse errors are raised to the caller, which is
expected to isolate them (see pipeline.run_scrapers).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("scrapers.archinect")

BASE_URL = "https://archinect.com"
JOBS_URL = "https://archinect.com/jobs"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20

# Design software worth tagging. Keyed by canonical tag -> match patterns.
SOFTWARE_TAGS: dict[str, tuple[str, ...]] = {
    "Revit": ("revit",),
    "Rhino": ("rhino", "rhinoceros"),
    "AutoCAD": ("autocad", "auto cad"),
    "SketchUp": ("sketchup", "sketch up"),
    "Grasshopper": ("grasshopper",),
}

# Keywords that mark a posting as an internship / co-op.
INTERN_PATTERNS = ("intern", "internship", "co-op", "coop", "co op")


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _is_internship(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in INTERN_PATTERNS)


def _software_tags(text: str) -> list[str]:
    low = text.lower()
    found = [tag for tag, patterns in SOFTWARE_TAGS.items() if any(p in low for p in patterns)]
    return found


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _parse_posted_date(card) -> str:
    """Best-effort extraction of a posting date; falls back to today.

    Archinect renders relative/absolute dates inconsistently across its job
    cards, so we look for a <time datetime="..."> first, then any text that
    parses as a date, and finally default to the scrape date.
    """
    time_el = card.find("time")
    if time_el is not None:
        dt_attr = time_el.get("datetime")
        if dt_attr:
            try:
                return datetime.fromisoformat(dt_attr.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                pass
        text = _clean(time_el.get_text())
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    return _today_iso()


def _parse_cards(html: str) -> list[dict]:
    """Parse the jobs index into listing dicts.

    Archinect's markup changes periodically; we defensively look for anchors
    that link to /jobs/ detail pages and pull structured bits from the
    surrounding card. Anything we can't parse is skipped rather than fatal.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings: list[dict] = []
    seen: set[str] = set()

    # Job cards on the index each contain a link to a /jobs/<id>/<slug> page.
    for link in soup.select("a[href*='/jobs/']"):
        href = link.get("href", "")
        # Skip category / pagination / the index itself.
        if not re.search(r"/jobs/\d+", href):
            continue

        title = _clean(link.get_text())
        if not title or not _is_internship(title):
            continue

        url = urljoin(BASE_URL, href)
        if url in seen:
            continue
        seen.add(url)

        # Company and location typically live in sibling elements of the card.
        card = link.find_parent(["li", "article", "div"]) or link.parent
        company = ""
        location = ""
        if card is not None:
            firm_el = card.select_one(".firm, .company, [class*='firm'], [class*='company']")
            loc_el = card.select_one(".location, [class*='location'], [class*='city']")
            company = _clean(firm_el.get_text()) if firm_el else ""
            location = _clean(loc_el.get_text()) if loc_el else ""

        posted_date = _parse_posted_date(card) if card is not None else _today_iso()
        tags = _software_tags(title)

        listings.append(
            {
                "id": _make_id(title, company),
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "posted_date": posted_date,
                "scraped_date": _today_iso(),
                "active": True,
                "tags": tags,
            }
        )

    return listings


def scrape() -> list[dict]:
    """Scrape Archinect internship/co-op postings. Returns a list of listings."""
    log.info("Fetching %s", JOBS_URL)
    html = _fetch(JOBS_URL)
    listings = _parse_cards(html)
    log.info("Parsed %d internship/co-op listing(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
