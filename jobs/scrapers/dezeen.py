"""Scraper for Dezeen Jobs (https://www.dezeenjobs.com).

Dezeen Jobs is a curated architecture/design board. It's server-rendered but
sits behind a WAF that fingerprints the TLS handshake and 403s stock Python
clients, so it's fetched via ``_http`` (curl_cffi browser impersonation).

Each listing is an ``article.job-info-block`` with a clean structure:
    .location-tag-list  -> location ("London, UK", "New York, USA")
    h1.entry-title       -> "<role>" (link to /job/) + " at " + "<company>"
    .job-list-blurb      -> a one-line description
    time.entry-date      -> "13 August 2026"
    .entry-meta          -> job-category tags

Feeds the ``architecture`` track. It's a broad board (many firms, many
disciplines), so ``require_keep=True`` + the seniority screen apply, and the
pipeline location gate keeps only target-market roles (Dezeen skews London/EU).

Independent and importable: ``from scrapers import dezeen`` then ``dezeen.scrape()``.
Errors raise to the caller (the pipeline isolates them).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from . import _employment, _filter, _http

log = logging.getLogger("scrapers.dezeen")

LIST_URL = "https://www.dezeenjobs.com/"
REQUEST_TIMEOUT = 30
_DATE_FMT = "%d %B %Y"  # "13 August 2026"


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


def _parse_article(art) -> dict | None:
    title_el = art.select_one("h1.entry-title")
    if title_el is None:
        return None
    job_link = title_el.find("a", href=re.compile(r"/job/"))
    if job_link is None:
        return None
    title = _clean(job_link.get_text())
    company_link = title_el.find("a", href=re.compile(r"/company/"))
    company = _clean(company_link.get_text()) if company_link else ""

    loc_el = art.select_one(".location-tag-list")
    # Tag text joins as "London , UK"; tidy the space before the comma.
    location = re.sub(r"\s+,", ",", _clean(loc_el.get_text(" "))) if loc_el else ""

    blurb_el = art.select_one(".job-list-blurb")
    blurb = ""
    if blurb_el is not None:
        blurb = _clean(re.sub(r"\bmore\b\s*$", "", blurb_el.get_text(" "), flags=re.I))

    # Broad board: require an entry-level signal on title+blurb; no senior titles.
    if not _filter.is_undergrad_accessible(f"{title} {blurb}", require_keep=True) \
            or _filter.has_seniority_term(title):
        return None

    date_el = art.select_one("time.entry-date")
    # Category tags, minus Dezeen's generic "... jobs" buckets (Design jobs,
    # Architecture jobs) which add no signal.
    category_tags = [
        c for a in art.select(".entry-meta a[href*='/job-category/']")
        if (c := _clean(a.get_text())) and not re.search(r"\bjobs$", c, re.I)
    ]
    emp = _employment.classify(title, text=blurb, default="Full-Time")
    tags = [emp] if emp else []
    for t in category_tags:
        if t and t not in tags:
            tags.append(t)

    return {
        "id": _make_id(title, company),
        "title": title,
        "company": company,
        "location": location,
        "url": job_link["href"],
        "posted_date": _parse_date(date_el.get_text()) if date_el else _today_iso(),
        "scraped_date": _today_iso(),
        "active": True,
        "tags": tags[:5],
        "_description": blurb,  # transient: consumed by the location gate
    }


def scrape() -> list[dict]:
    """Scrape Dezeen Jobs. Returns a list of listing dicts."""
    resp = _http.get(LIST_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    by_id: dict[str, dict] = {}
    for art in soup.select("article.job-info-block"):
        item = _parse_article(art)
        if item is not None:
            by_id.setdefault(item["id"], item)

    listings = list(by_id.values())
    log.info("Dezeen: %d undergrad-accessible listing(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
