"""Scraper for Harvard Psychology's Post-Graduate Research Jobs board.

`undergrad.psychology.fas.harvard.edu/post-graduate-research-jobs` is a curated,
department-listed board of post-baccalaureate research jobs (RA, lab coordinator,
research associate) -- the natural next step for the undergrads this board serves.
It's server-rendered but behind a WAF that 403s stock Python clients, so it's
fetched via ``_http`` (curl_cffi browser impersonation).

Each posting is a two-cell table row: a date ("Aug 13, 2026") and a title that
names the role, lab, and host institution ("Research Associate in the Shuffrey
Lab at New York University"), linking to a PDF flyer. The institution is the only
location signal, so it's pre-resolved with the shared matcher (catches e.g. "New
York University" -> NYC without an LLM call) and otherwise left for the pipeline's
location gate to resolve from the title text.

Curated source: ``require_keep=False`` (only the advanced-degree DROP screen).
Feeds the ``psychology`` track. Errors raise to the caller (pipeline isolates).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import _employment, _filter, _http, _location

log = logging.getLogger("scrapers.harvard")

BASE_URL = "https://undergrad.psychology.fas.harvard.edu"
PAGE_URL = f"{BASE_URL}/post-graduate-research-jobs"
REQUEST_TIMEOUT = 30
_DATE_FMT = "%b %d, %Y"  # "Aug 13, 2026"


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


def _split_title(title: str) -> tuple[str, str]:
    """Split "<role> ... at <institution>" into (role, institution)."""
    m = re.search(r"\b(?:located at|at)\s+(.+)$", title, re.IGNORECASE)
    if m and m.start() > 0:
        return _clean(title[: m.start()]), _clean(m.group(1))
    return title, ""


def _parse_row(row) -> dict | None:
    cells = row.find_all("td")
    if len(cells) < 2:
        return None  # header row
    date_text = _clean(cells[0].get_text())
    title = _clean(cells[1].get_text(" "))
    if not title:
        return None
    if not _filter.is_undergrad_accessible(title, require_keep=False):
        return None

    anchor = cells[1].find("a", href=True)
    url = urljoin(BASE_URL, anchor["href"]) if anchor else PAGE_URL
    role, company = _split_title(title)
    # Pre-resolve the institution to a target market when the rules can (avoids
    # an LLM call); otherwise leave location empty and let the gate mine the
    # title. ``_description`` carries the title so the gate has something to read.
    label = _location.target_label(title)
    emp = _employment.classify(role, text=title, default="Full-Time")
    tags = [emp] if emp else []
    if "Research" not in tags:
        tags.append("Research")
    return {
        "id": _make_id(role, company),
        "title": role,
        "company": company,
        "location": label or "",
        "url": url,
        "posted_date": _parse_date(date_text),
        "scraped_date": _today_iso(),
        "active": True,
        "tags": tags,
        "_description": title,  # transient: consumed by the location gate
    }


def scrape() -> list[dict]:
    """Scrape Harvard's post-grad research jobs board. Returns listing dicts."""
    resp = _http.get(PAGE_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    table = soup.find("table")
    if table is None:
        log.warning("Harvard: no postings table found")
        return []

    by_id: dict[str, dict] = {}
    for row in table.find_all("tr"):
        item = _parse_row(row)
        if item is not None:
            by_id.setdefault(item["id"], item)

    listings = list(by_id.values())
    log.info("Harvard: %d post-grad research listing(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
