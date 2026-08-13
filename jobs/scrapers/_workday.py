"""Reusable Workday job-board adapter.

Workday-hosted career sites render through a hidden JSON API (the same one their
React front-end calls), so no headless browser is needed. Given a firm's host /
tenant / site, this POSTs to that API, paginates, and maps each posting to the
shared listing schema.

    POST https://{host}/wday/cxs/{tenant}/{site}/jobs
    body {"appliedFacets":{},"limit":20,"offset":0,"searchText":"intern"}

Fields used: ``title``, ``locationsText``, ``postedOn`` (relative, e.g. "Posted
3 Days Ago"), ``externalPath`` (-> public job URL). Response ``total`` drives
pagination by ``offset``.

Undergrad filtering is applied here (self-contained), on the job **title** with
the seniority screen, matching the Greenhouse adapter. Not a pipeline entry point
-- ``scrapers/firms.py`` calls ``fetch_board`` per firm. Errors raise to the
caller (the pipeline isolates them).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from . import _employment, _filter

log = logging.getLogger("scrapers.workday")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 20          # Workday's max page size for this endpoint
MAX_PAGES = 25           # safety cap (500 postings) so a bad total can't loop
PAGE_DELAY_SECONDS = 0.4  # be polite between paginated POSTs


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _today_iso() -> str:
    return _today().date().isoformat()


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _parse_posted_on(raw: str | None) -> str:
    """Convert Workday's relative ``postedOn`` to an ISO date.

    Examples: "Posted Today"/"Posted Yesterday", "Posted 3 Days Ago",
    "Posted 30+ Days Ago", "Posted 2 Months Ago". Anything unparseable -> today.
    """
    text = (raw or "").lower()
    if not text or "today" in text or "just posted" in text:
        return _today_iso()
    if "yesterday" in text:
        return (_today() - timedelta(days=1)).date().isoformat()
    m = re.search(r"(\d+)", text)
    if m and "day" in text:
        return (_today() - timedelta(days=int(m.group(1)))).date().isoformat()
    if m and "month" in text:
        return (_today() - timedelta(days=int(m.group(1)) * 30)).date().isoformat()
    return _today_iso()


def _tags_from(mapping: dict[str, tuple[str, ...]] | None, text: str) -> list[str]:
    if not mapping:
        return []
    low = text.lower()
    return [tag for tag, pats in mapping.items() if any(p in low for p in pats)]


def fetch_board(
    host: str,
    tenant: str,
    site: str,
    *,
    company: str,
    require_keep: bool = True,
    search_text: str = "intern",
    extra_tags: list[str] | None = None,
    tag_mapping: dict[str, tuple[str, ...]] | None = None,
) -> list[dict]:
    """Fetch a Workday board and return undergrad-accessible listing dicts.

    ``company`` is the firm's display name (Workday's API carries no company
    field). ``search_text`` is the server-side full-text query.
    """
    api_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    job_base = f"https://{host}/en-US/{site}"
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    by_id: dict[str, dict] = {}
    offset = 0
    total = None
    for page in range(MAX_PAGES):
        resp = session.post(
            api_url,
            json={
                "appliedFacets": {},
                "limit": PAGE_LIMIT,
                "offset": offset,
                "searchText": search_text,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if total is None:
            total = data.get("total") or 0
            log.info("Workday %s/%s: %s total for %r", tenant, site, total, search_text)
        postings = data.get("jobPostings") or []
        if not postings:
            break

        for job in postings:
            title = _clean(job.get("title"))
            if not title:
                continue
            if not _filter.is_undergrad_accessible(
                title, require_keep=require_keep, drop_seniority=require_keep
            ):
                continue
            path = job.get("externalPath") or ""
            url = f"{job_base}{path}" if path else job_base
            # Workday's list API carries no employment-type field; default firm
            # postings to Full-Time (intern titles still classify as Internship).
            emp = _employment.classify(title, default="Full-Time")
            tags = [emp] if emp else []
            for t in _tags_from(tag_mapping, title) + (extra_tags or []):
                if t not in tags:
                    tags.append(t)
            listing = {
                "id": _make_id(title, company),
                "title": title,
                "company": company,
                "location": _clean(job.get("locationsText")),
                "url": url,
                "posted_date": _parse_posted_on(job.get("postedOn")),
                "scraped_date": _today_iso(),
                "active": True,
                "tags": tags,
            }
            by_id.setdefault(listing["id"], listing)

        offset += PAGE_LIMIT
        if offset >= (total or 0):
            break
        time.sleep(PAGE_DELAY_SECONDS)

    listings = list(by_id.values())
    log.info("Workday %s/%s: %d undergrad-accessible listing(s)", tenant, site, len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(
        fetch_board(
            "gensler.wd1.myworkdayjobs.com", "gensler", "genslercareers",
            company="Gensler",
        ),
        indent=2, ensure_ascii=False,
    ))
