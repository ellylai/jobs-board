"""Scraper for Duke Psychology & Neuroscience's opportunities board.

`psychandneuro.duke.edu/undergraduate/research-opportunities-jobs` is a curated,
server-rendered Drupal list of internships, paid jobs, and research roles that the
department vets for its (undergraduate) students -- squarely this board's audience.
Each opportunity is a ``.views-row`` with a ``.views-field-title`` ("<role> @
<org>", linking to the detail page) and a ``.views-field-view-node`` snippet.

Curated source: ``require_keep=False`` (only the advanced-degree/licensure DROP
screen applies). Study-*recruitment* posts (seeking research participants, not
hiring) are skipped -- they're on the same list but aren't jobs. The page carries
no post dates, so ``posted_date`` is left empty rather than faked to "today"
(which would flag every listing 🆕 forever). Feeds the ``psychology`` track.

Independent and importable: ``from scrapers import duke`` then ``duke.scrape()``.
Errors raise to the caller (the pipeline isolates them).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import _employment, _filter

log = logging.getLogger("scrapers.duke")

BASE_URL = "https://psychandneuro.duke.edu"
PAGE_URL = f"{BASE_URL}/undergraduate/research-opportunities-jobs"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30

# Titles of study-recruitment posts (not jobs) to skip.
_RECRUITMENT_RE = re.compile(r"participat|participants|study volunteers", re.IGNORECASE)
# Trailing "read more about this opportunity »" boilerplate on the snippet.
_READMORE_RE = re.compile(r"\bread\s+more\b.*$", re.IGNORECASE | re.DOTALL)

# Domain tags (employment type is handled separately by _employment). The page's
# own type facets aren't exposed per row, so these are keyword-based.
TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Paid": ("paid", "salary", "stipend", "hourly"),
    "Research": ("research", "lab ", "laboratory"),
}

US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_CITY_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z.]+(?:\s[A-Z][a-zA-Z.]+)*),?\s+([A-Z]{2})\b")


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean(text: str | None) -> str:
    # Normalize the non-breaking hyphen (U+2011) these titles use in "Full-time".
    text = (text or "").replace("‑", "-")
    return re.sub(r"\s+", " ", text).strip()


def _split_title(raw: str) -> tuple[str, str]:
    """Split "<role> @ <org>" into (role, org). Falls back to (raw, "")."""
    if " @ " in raw:
        role, org = raw.split(" @ ", 1)
        return role.strip(), org.strip()
    return raw, ""


def _tags_from(text: str) -> list[str]:
    low = text.lower()
    tags = [tag for tag, pats in TAG_KEYWORDS.items() if any(p in low for p in pats)]
    return tags[:4]


def _location_from(text: str) -> str:
    for city, st in _CITY_STATE_RE.findall(text):
        if st in US_STATE_ABBR:
            return f"{city}, {st}"
    return ""


def _parse_row(row) -> dict | None:
    title_field = row.select_one(".views-field-title")
    if title_field is None:
        return None
    raw_title = _clean(title_field.get_text(" "))
    if not raw_title or _RECRUITMENT_RE.search(raw_title):
        return None

    anchor = title_field.find("a", href=True)
    node_field = row.select_one(".views-field-view-node")
    body = _clean(node_field.get_text(" ")) if node_field else ""
    body = _READMORE_RE.sub("", body).strip()

    # Curated source: only the advanced-degree/licensure DROP screen applies.
    if not _filter.is_undergrad_accessible(f"{raw_title} {body}", require_keep=False):
        return None

    role, org = _split_title(raw_title)
    url = urljoin(BASE_URL, anchor["href"]) if anchor else PAGE_URL
    emp = _employment.classify(role, text=body)
    tags = ([emp] if emp else []) + _tags_from(f"{raw_title} {body}")
    return {
        "id": _make_id(role, org),
        "title": role,
        "company": org,
        "location": _location_from(org) or _location_from(body),
        "url": url,
        # The page publishes no post dates; leave empty (see module docstring).
        "posted_date": "",
        "scraped_date": _today_iso(),
        "active": True,
        "tags": tags,
        "_description": body,  # transient: consumed by the location gate
    }


def scrape() -> list[dict]:
    """Scrape Duke's opportunities board. Returns a list of listing dicts."""
    resp = requests.get(
        PAGE_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    by_id: dict[str, dict] = {}
    for row in soup.select(".views-row"):
        item = _parse_row(row)
        if item is not None:
            by_id.setdefault(item["id"], item)

    listings = list(by_id.values())
    log.info("Duke: %d opportunity listing(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
