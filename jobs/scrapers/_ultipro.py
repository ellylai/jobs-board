"""Reusable UKG/UltiPro (recruiting.ultipro.com) job-board adapter.

UltiPro job boards expose a clean JSON search endpoint behind the React UI:

    POST https://{host}/{code}/JobBoard/{board_id}/JobBoardView/LoadSearchResults

Each opportunity carries ``Title``, ``Locations`` (list of ``LocalizedName``),
``PostedDate`` (ISO), ``FullTime`` (bool), ``BriefDescription``, and an ``Id``
used to build the detail URL. Adding an UltiPro firm is one config entry
(host/code/board_id, read from its careers URL
``https://{host}/{code}/JobBoard/{board_id}/``).

Undergrad filtering is applied here (broad source: ``require_keep=True`` +
seniority screen, on the title). Not a pipeline entry point --
``scrapers/firms.py`` calls ``fetch_board``. Errors raise to the caller.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

import requests

from . import _employment, _filter

log = logging.getLogger("scrapers.ultipro")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
PAGE_SIZE = 100

# The search body the endpoint expects; empty filters return the whole board,
# most-recent first.
def _search_body(top: int, skip: int) -> dict:
    return {
        "opportunitySearch": {
            "Top": top, "Skip": skip, "QueryString": "",
            "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate",
                         "Ascending": False}],
            "Filters": [],
        },
        "matchCriteria": {
            "PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
            "Skills": [], "WorkExperiences": [], "DesiredPay": None,
            "Locations": [], "JobCategories": [], "WorkPreferences": [],
            "PayCycle": None, "KeyWordSearchText": None,
        },
    }


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


def _location_of(loc: dict) -> str:
    """Build "City, ST" from an UltiPro location's Address (LocalizedName is null)."""
    addr = loc.get("Address") or {}
    city = _clean(addr.get("City"))
    state = _clean((addr.get("State") or {}).get("Code"))
    if city and state:
        return f"{city}, {state}"
    return city or _clean(loc.get("LocalizedDescription") or loc.get("DisplayName"))


def _locations(job: dict) -> str:
    names = [_location_of(loc) for loc in (job.get("Locations") or [])]
    return "; ".join(dict.fromkeys(n for n in names if n))  # de-dup, keep order


def fetch_board(
    code: str,
    board_id: str,
    *,
    company: str,
    host: str = "recruiting2.ultipro.com",
    require_keep: bool = True,
    extra_tags: list[str] | None = None,
    tag_mapping: dict[str, tuple[str, ...]] | None = None,
) -> list[dict]:
    """Fetch an UltiPro board and return undergrad-accessible listing dicts."""
    api = f"https://{host}/{code}/JobBoard/{board_id}/JobBoardView/LoadSearchResults"
    detail = f"https://{host}/{code}/JobBoard/{board_id}/OpportunityDetail?opportunityId={{id}}"
    resp = requests.post(
        api,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                 "Accept": "application/json"},
        json=_search_body(PAGE_SIZE, 0),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    opportunities = resp.json().get("opportunities") or []

    by_id: dict[str, dict] = {}
    for job in opportunities:
        title = _clean(job.get("Title"))
        if not title:
            continue
        if not _filter.is_undergrad_accessible(
            title, require_keep=require_keep, drop_seniority=require_keep
        ):
            continue
        brief = _clean(re.sub(r"<[^>]+>", " ", job.get("BriefDescription") or ""))
        default_emp = "Full-Time" if job.get("FullTime") else ""
        emp = _employment.classify(title, text=brief, default=default_emp)
        tags = [emp] if emp else []
        for t in _tags_from(tag_mapping, f"{title} {brief}") + (extra_tags or []):
            if t not in tags:
                tags.append(t)
        posted = job.get("PostedDate") or ""
        listing = {
            "id": _make_id(title, company),
            "title": title,
            "company": company,
            "location": _locations(job),
            "url": detail.format(id=job.get("Id", "")),
            "posted_date": posted[:10] if posted else _today_iso(),
            "scraped_date": _today_iso(),
            "active": True,
            "tags": tags,
            "_description": brief,  # transient: consumed by the location gate
        }
        by_id.setdefault(listing["id"], listing)

    listings = list(by_id.values())
    log.info("UltiPro %s: %d undergrad-accessible of %d", code, len(listings), len(opportunities))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(
        fetch_board("PER1007PWILL", "0ca393a4-bf82-4db6-acae-91e6a0315a4a",
                    company="Perkins&Will"),
        indent=2, ensure_ascii=False,
    ))
