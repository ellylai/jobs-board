"""Reusable Jobvite job-board adapter.

Jobvite hosts firm career pages at ``jobs.jobvite.com/{slug}/search`` and renders
the listing table server-side, so adding a Jobvite firm is one config entry (its
slug). Each job is an ``a.flex-row`` linking to ``/{slug}/job/{id}`` with a
``.jv-job-list-name`` (title) and ``.jv-job-list-location`` (one or more offices).

The list page carries no post date or description, but the office location is
reliable -- enough for the pipeline location gate without an LLM call. Undergrad
filtering is applied here (broad source: ``require_keep=True`` + seniority screen,
on the title).

Not a pipeline entry point -- ``scrapers/firms.py`` calls ``fetch_board``. Errors
raise to the caller (the pipeline isolates them).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

from . import _employment, _filter

log = logging.getLogger("scrapers.jobvite")

BASE_URL = "https://jobs.jobvite.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _tags_from(mapping: dict[str, tuple[str, ...]] | None, text: str) -> list[str]:
    if not mapping:
        return []
    low = text.lower()
    return [tag for tag, pats in mapping.items() if any(p in low for p in pats)]


def fetch_board(
    slug: str,
    *,
    company: str,
    require_keep: bool = True,
    extra_tags: list[str] | None = None,
    tag_mapping: dict[str, tuple[str, ...]] | None = None,
) -> list[dict]:
    """Fetch a Jobvite board and return undergrad-accessible listing dicts."""
    resp = requests.get(
        f"{BASE_URL}/{slug}/search",
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(resp.content, "html.parser")

    by_id: dict[str, dict] = {}
    for anchor in soup.select(f"a[href*='/{slug}/job/']"):
        name_el = anchor.select_one(".jv-job-list-name")
        if name_el is None:
            continue
        title = _clean(name_el.get_text())
        if not title:
            continue
        if not _filter.is_undergrad_accessible(
            title, require_keep=require_keep, drop_seniority=require_keep
        ):
            continue
        loc_el = anchor.select_one(".jv-job-list-location")
        location = _clean(loc_el.get_text(" ")) if loc_el else ""

        emp = _employment.classify(title, default="Full-Time")
        tags = [emp] if emp else []
        for t in _tags_from(tag_mapping, title) + (extra_tags or []):
            if t not in tags:
                tags.append(t)

        listing = {
            "id": _make_id(title, company),
            "title": title,
            "company": company,
            "location": location,
            "url": urljoin(BASE_URL, anchor["href"]),
            "posted_date": "",  # the Jobvite list page shows no post date
            "scraped_date": _today_iso(),
            "active": True,
            "tags": tags,
        }
        by_id.setdefault(listing["id"], listing)

    listings = list(by_id.values())
    log.info("Jobvite %s: %d undergrad-accessible listing(s)", slug, len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    import sys

    slug = sys.argv[1] if len(sys.argv) > 1 else "nbbj"
    print(json.dumps(fetch_board(slug, company=slug.upper()), indent=2, ensure_ascii=False))
