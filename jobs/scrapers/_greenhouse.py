"""Reusable Greenhouse job-board adapter.

Greenhouse hosts many firms' careers pages and exposes a clean public JSON API,
so adding a firm on Greenhouse is one config entry (its board *slug*) rather than
a bespoke scraper. Given a slug, this fetches the whole board and maps each
posting to the shared listing schema.

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

Fields used: ``title``, ``location.name``, ``absolute_url``, ``company_name``,
``first_published`` (-> posted_date), ``content`` (HTML, used only for tagging).

Undergrad filtering is applied here (the adapter is self-contained). Firm boards
are a *broad* source, so callers pass ``require_keep=True``; filtering runs on the
job **title** (not the HTML body, whose boilerplate leaks stray keywords) with the
seniority screen enabled.

Not a pipeline entry point itself -- ``scrapers/firms.py`` calls ``fetch_board``
per firm. Network/parse errors raise to the caller (the pipeline isolates them).
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timezone

import requests

from . import _employment, _filter

log = logging.getLogger("scrapers.greenhouse")

API_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
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


def _iso_date(value: str | None) -> str:
    """Reduce Greenhouse's ISO datetime (with tz offset) to a plain ISO date."""
    if not value:
        return _today_iso()
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return (value[:10] if len(value) >= 10 else "") or _today_iso()


def _strip_html(content: str | None) -> str:
    """Greenhouse ``content`` is HTML-entity-encoded markup; return plain text."""
    if not content:
        return ""
    return _clean(re.sub(r"<[^>]+>", " ", html.unescape(content)))


def _tags_from(mapping: dict[str, tuple[str, ...]] | None, text: str) -> list[str]:
    if not mapping:
        return []
    low = text.lower()
    return [tag for tag, pats in mapping.items() if any(p in low for p in pats)]


def fetch_board(
    slug: str,
    *,
    company: str | None = None,
    require_keep: bool = True,
    extra_tags: list[str] | None = None,
    tag_mapping: dict[str, tuple[str, ...]] | None = None,
) -> list[dict]:
    """Fetch a Greenhouse board and return undergrad-accessible listing dicts.

    ``company`` overrides the board's ``company_name`` for display/id. ``extra_tags``
    are appended to every listing; ``tag_mapping`` extracts tags from title+body.
    """
    resp = requests.get(
        API_URL.format(slug=slug),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    jobs = resp.json().get("jobs", []) or []

    by_id: dict[str, dict] = {}
    for job in jobs:
        title = _clean(job.get("title"))
        if not title:
            continue
        # Broad source: filter on the title only (the HTML body's boilerplate
        # leaks stray keep-words), with the seniority screen on.
        if not _filter.is_undergrad_accessible(
            title, require_keep=require_keep, drop_seniority=require_keep
        ):
            continue

        firm = company or _clean(job.get("company_name")) or slug
        location = _clean((job.get("location") or {}).get("name"))
        body = _strip_html(job.get("content"))
        # Firm boards expose no employment-type field; a design firm's non-intern
        # postings are salaried professional roles, so default to Full-Time.
        emp = _employment.classify(title, text=body, default="Full-Time")
        tags = [emp] if emp else []
        for t in _tags_from(tag_mapping, f"{title} {body}") + (extra_tags or []):
            if t not in tags:
                tags.append(t)

        listing = {
            "id": _make_id(title, firm),
            "title": title,
            "company": firm,
            "location": location,
            "url": job.get("absolute_url") or "",
            "posted_date": _iso_date(job.get("first_published")),
            "scraped_date": _today_iso(),
            "active": True,
            "tags": tags,
            # No _description: a firm board's structured location is reliable, and
            # its HTML body is office-list/legal boilerplate that would mislead the
            # location gate (every office city + "remote options" + CA privacy text).
        }
        by_id.setdefault(listing["id"], listing)

    listings = list(by_id.values())
    log.info("Greenhouse %s: %d undergrad-accessible of %d posting(s)",
             slug, len(listings), len(jobs))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    import sys

    board = sys.argv[1] if len(sys.argv) > 1 else "dlrgroup"
    print(json.dumps(fetch_board(board), indent=2, ensure_ascii=False))
