"""Scraper for the APPIC Directory (https://www.appic.org/Directory).

APPIC (the Association of Psychology Postdoctoral and Internship Centers) hosts
the canonical directory of doctoral psychology internship programs. The public
search lives at membership.appic.org and, despite the jQuery-heavy UI, renders
its results table server-side -- so no headless browser is needed. The one
wrinkle: the search only responds once a per-session ``new_id`` token (issued on
the search page) is echoed back, alongside the session cookies.

Feeds the ``psychology`` track. Each program row maps to a listing:
    company  <- the training site
    title    <- the specific program / department name
    location <- city, state, country
    posted_date <- the program's "Last Updated" date (APPIC has no post date;
                   this is the best available recency signal)
Tags capture the training setting (clinical/research/school/community, parsed
from the program name) and the application due date.

Independent and importable: ``from scrapers import appic`` then
``appic.scrape()``. Errors raise to the caller (pipeline isolates them).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("scrapers.appic")

BASE_URL = "https://membership.appic.org"
SEARCH_URL = "https://membership.appic.org/directory/search"
PROGRAM_TYPE_INTERNSHIP = "1"  # program_type_id: 1 = Internship, 2 = Post Doctoral
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
MAX_PAGES = 40          # safety cap; the directory is ~34 pages of 25 rows
PAGE_DELAY_SECONDS = 0.5  # be polite between page fetches

# Training-setting keywords to tag, parsed from the program/department name.
SETTING_TAGS: dict[str, tuple[str, ...]] = {
    "clinical": ("clinical", "hospital", "medical", "health", "counseling center"),
    "research": ("research", "academic"),
    "school": ("school", "university", "college", "education"),
    "community": ("community", "correctional", "va ", "veterans", "prison"),
    "forensic": ("forensic", "correctional", "prison"),
    "neuropsych": ("neuropsych",),
}


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _parse_us_date(text: str) -> str | None:
    """Parse APPIC's MM/DD/YYYY dates (ignoring any trailing time) to ISO."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _split_site_department(text: str) -> tuple[str, str]:
    """Split "Site / Department" into (company, title).

    Site names can contain slashes without surrounding spaces (e.g. "Children's
    Hospital Stanford/Children's Health Council"); APPIC uses " / " (spaced) to
    separate the site from the department, so split on that.
    """
    parts = re.split(r"\s+/\s+", text, maxsplit=1)
    if len(parts) == 2:
        return _clean(parts[0]), _clean(parts[1])
    return _clean(text), "Internship Program"


def _setting_tags(title: str, company: str) -> list[str]:
    hay = f"{title} {company}".lower()
    return [tag for tag, pats in SETTING_TAGS.items() if any(p in hay for p in pats)]


def _new_id(session: requests.Session) -> str:
    """Fetch the search page to seed session cookies and read the new_id token."""
    resp = session.get(SEARCH_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    field = BeautifulSoup(resp.content, "html.parser").find("input", {"name": "new_id"})
    if not field or not field.get("value"):
        raise RuntimeError("APPIC: could not find new_id token on search page")
    return field["value"]


def _fetch_page(session: requests.Session, new_id: str, page: int) -> bytes:
    resp = session.get(
        SEARCH_URL,
        params={
            "search": "1",
            "new_id": new_id,
            "program_type_id": PROGRAM_TYPE_INTERNSHIP,
            "p": page,
        },
        headers={"Referer": SEARCH_URL},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.content


def _total_pages(content: bytes) -> int:
    """Read "Pages (1 of N)" from the results page; default to 1 if absent.

    APPIC renders this inside an HTML comment (``...Pages (1 of 34):</span-->``),
    so parse the raw markup rather than BeautifulSoup's stripped text.
    """
    raw = content.decode("utf-8", errors="replace")
    m = re.search(r"Pages\s*\(\s*\d+\s*of\s*(\d+)\s*\)", raw, re.I)
    return int(m.group(1)) if m else 1


def _parse_rows(content: bytes) -> list[dict]:
    soup = BeautifulSoup(content, "html.parser")
    table = soup.find("table")
    if table is None:
        return []

    listings: list[dict] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue  # header or malformed row

        site_cell = cells[1]
        anchor = site_cell.find("a", href=True)
        site_text = _clean(site_cell.get_text(" "))
        if not site_text:
            continue

        company, title = _split_site_department(site_text)
        location = _clean(cells[2].get_text(" "))
        due_date = _parse_us_date(cells[3].get_text())
        last_updated = _parse_us_date(cells[5].get_text())
        url = urljoin(BASE_URL, anchor["href"]) if anchor else SEARCH_URL

        tags = _setting_tags(title, company)
        if due_date:
            tags.append(f"Due {due_date}")

        listings.append(
            {
                "id": _make_id(title, company),
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "posted_date": last_updated or _today_iso(),
                "scraped_date": _today_iso(),
                "active": True,
                "tags": tags,
            }
        )

    return listings


def scrape() -> list[dict]:
    """Scrape APPIC internship programs. Returns a list of listing dicts."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    new_id = _new_id(session)
    log.info("APPIC search token acquired; fetching page 1")

    first = _fetch_page(session, new_id, 1)
    pages = min(_total_pages(first), MAX_PAGES)
    log.info("APPIC reports %d result page(s)", pages)

    by_id: dict[str, dict] = {}
    for item in _parse_rows(first):
        by_id.setdefault(item["id"], item)

    for page in range(2, pages + 1):
        time.sleep(PAGE_DELAY_SECONDS)
        try:
            content = _fetch_page(session, new_id, page)
        except requests.RequestException as exc:
            log.warning("APPIC page %d failed (%s); stopping pagination", page, exc)
            break
        rows = _parse_rows(content)
        if not rows:
            break
        before = len(by_id)
        for item in rows:
            by_id.setdefault(item["id"], item)
        if len(by_id) == before:
            # No new listings -- likely paged past the end (APPIC wraps rather
            # than 404ing). Stop to avoid looping to MAX_PAGES needlessly.
            break

    listings = list(by_id.values())
    log.info("Parsed %d APPIC internship program(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
