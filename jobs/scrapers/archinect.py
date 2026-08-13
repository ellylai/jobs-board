"""Scraper for Archinect jobs (https://archinect.com/jobs).

Queries Archinect's job search for internship / co-op terms, then keeps only
postings whose *title* actually reads as an internship/co-op (the search also
matches on body text, so it returns unrelated senior roles too). Extracts
title, company, location, url, and posted_date, and tags any design software
mentioned in the title.

Independent and importable: ``from scrapers import archinect`` then
``archinect.scrape()``. Network/parse errors are raised to the caller, which is
expected to isolate them (see pipeline.run_scrapers).
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import _employment

log = logging.getLogger("scrapers.archinect")

BASE_URL = "https://archinect.com"
SEARCH_URL = "https://archinect.com/jobs/search"
# Search terms to union. The title filter below removes false positives.
SEARCH_TERMS = ("internship", "co-op")
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

# Keywords that mark a posting title as an internship / co-op.
INTERN_PATTERNS = ("intern", "internship", "co-op", "coop", "co op")

# Archinect renders dates like "Thu, Aug 13 '26".
_DATE_FMT = "%a, %b %d '%y"


# --- Mixed-encoding decode -------------------------------------------------
# Archinect serves UTF-8 pages that occasionally contain stray Windows-1252
# bytes (e.g. an en-dash 0x96). Decode as UTF-8 but fall back to cp1252 for any
# invalid byte instead of producing U+FFFD replacement characters.
def _mixed_error_handler(err: UnicodeDecodeError):
    bad = err.object[err.start:err.end]
    return bad.decode("cp1252", errors="replace"), err.end


codecs.register_error("archinect_mixed", _mixed_error_handler)


def _decode(content: bytes) -> str:
    return content.decode("utf-8", errors="archinect_mixed")


# --- Small helpers ---------------------------------------------------------
def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _is_internship(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in INTERN_PATTERNS)


def _software_tags(text: str) -> list[str]:
    low = text.lower()
    return [tag for tag, pats in SOFTWARE_TAGS.items() if any(p in low for p in pats)]


def _parse_date(span_text: str) -> str:
    text = _clean(span_text)
    try:
        return datetime.strptime(text, _DATE_FMT).date().isoformat()
    except ValueError:
        return _today_iso()


def _fetch(term: str) -> bytes:
    resp = requests.get(
        SEARCH_URL,
        params={"q": term},
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.content


# --- Parsing ---------------------------------------------------------------
def _parse_entry(entry) -> dict | None:
    """Parse one ``li.Entry`` card into a listing dict, or None to skip it."""
    anchor = entry.find("a", href=True)
    if anchor is None:
        return None

    title = _clean(anchor.get("title") or "")
    if not title:
        h1 = entry.select_one(".Col1 h1")
        title = _clean(h1.get_text()) if h1 else ""
    if not title or not _is_internship(title):
        return None

    company_el = entry.select_one(".Col1 h2")
    location_el = entry.select_one(".Col2 p")
    date_el = entry.select_one(".Col2 span")

    company = _clean(company_el.get_text()) if company_el else ""
    location = _clean(location_el.get_text()) if location_el else ""
    posted_date = _parse_date(date_el.get_text()) if date_el else _today_iso()
    url = urljoin(BASE_URL, anchor["href"])

    emp = _employment.classify(title, default="Internship")
    tags = ([emp] if emp else []) + _software_tags(title)
    return {
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


def _parse_search(content: bytes) -> list[dict]:
    soup = BeautifulSoup(_decode(content), "html.parser")
    listings = []
    for entry in soup.select("li.Entry"):
        item = _parse_entry(entry)
        if item is not None:
            listings.append(item)
    return listings


def scrape() -> list[dict]:
    """Scrape Archinect internship/co-op postings. Returns a list of listings."""
    by_id: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        log.info("Searching Archinect jobs for %r", term)
        for item in _parse_search(_fetch(term)):
            by_id.setdefault(item["id"], item)  # dedup across terms
    listings = list(by_id.values())
    log.info("Parsed %d internship/co-op listing(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
