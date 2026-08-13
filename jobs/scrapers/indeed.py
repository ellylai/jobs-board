"""Job search via SerpAPI's Google Jobs engine.

Named ``indeed`` for the track's original intent, but it deliberately does NOT
scrape Indeed directly (that violates their ToS and is brittle). Instead it
queries Google Jobs through SerpAPI, which aggregates Indeed, LinkedIn, company
sites, etc. Requires a ``SERPAPI_KEY`` environment variable (a GitHub Actions
secret in CI).

This board targets undergraduates, so the psychology queries hunt for
entry-level roles (research assistant, lab intern, clinical aide, counseling
center volunteer, rehab center intern, behavioral health intern) and a filter
drops anything that requires an advanced degree or seniority.

Exposes two track-specific entry points used by the pipeline:
    scrape_architecture() -> list[dict]
    scrape_psychology()   -> list[dict]

If ``SERPAPI_KEY`` is unset, both return [] with a warning rather than raising,
so the pipeline degrades gracefully when the secret is missing.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from . import _employment, _filter

try:
    from serpapi import GoogleSearch
except ImportError:  # package name: google-search-results
    GoogleSearch = None  # handled at call time

log = logging.getLogger("scrapers.indeed")

SERPAPI_KEY_ENV = "SERPAPI_KEY"
# Default location biases results to the US; Google Jobs still returns remote.
DEFAULT_LOCATION = "United States"
# Pages of ~10 results to pull per query. Each page is one SerpAPI search
# (counts against quota), so keep this small.
MAX_PAGES_PER_QUERY = 1

# Undergrad-accessible psychology role searches. Kept to 4 queries (= 4 SerpAPI
# searches/run, ~120/month daily) to stay under the free tier's 250/month.
# OR-groups fold several of the requested role types into one search each.
PSYCH_QUERIES = (
    "psychology internship",
    "psychology research assistant OR lab intern",
    "clinical aide OR behavioral health intern",
    "counseling center volunteer OR rehabilitation intern",
)

ARCH_QUERIES = (
    "architecture internship",
    "architecture co-op",
)

# Role-category (domain) tags for psychology listings. Title-cased to match the
# employment-type tag; "volunteer"/"remote" are omitted here since the
# employment-type and location fields already carry them.
PSYCH_ROLE_TAGS: dict[str, tuple[str, ...]] = {
    "Research": ("research assistant", "research intern", "lab ", "laboratory"),
    "Clinical": ("clinical aide", "clinical intern", "clinic"),
    "Counseling": ("counsel", "counseling"),
    "Rehab": ("rehab", "rehabilitation"),
    "Behavioral": ("behavioral", "behavior tech", "rbt", "aba "),
}

# Design software tags for architecture listings (mirrors archinect.py).
ARCH_SOFTWARE_TAGS: dict[str, tuple[str, ...]] = {
    "Revit": ("revit",),
    "Rhino": ("rhino",),
    "AutoCAD": ("autocad", "auto cad"),
    "SketchUp": ("sketchup", "sketch up"),
    "Grasshopper": ("grasshopper",),
}


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _today_iso() -> str:
    return _today().date().isoformat()


def _clean(text: str | None) -> str:
    # Google Jobs returns HTML-entity-encoded text ("&amp;", "&#39;"); decode it.
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _parse_posted_date(detected: dict | None) -> str:
    """Convert Google Jobs' relative 'posted_at' to an ISO date.

    Examples: "Just posted"/"Today" -> today; "3 days ago" -> today-3;
    "30+ days ago" -> today-30; anything unparseable -> today.
    """
    if not detected:
        return _today_iso()
    raw = (detected.get("posted_at") or "").lower()
    if not raw or "today" in raw or "just posted" in raw or "hour" in raw:
        return _today_iso()
    m = re.search(r"(\d+)", raw)
    if m and "day" in raw:
        return (_today() - timedelta(days=int(m.group(1)))).date().isoformat()
    if m and ("month" in raw):
        return (_today() - timedelta(days=int(m.group(1)) * 30)).date().isoformat()
    return _today_iso()


def _is_undergrad_accessible(title: str, description: str) -> bool:
    """Undergrad-accessibility for a web-search hit (a *broad* source).

    Uses the shared filter: title+description must carry an entry-level KEEP
    signal and no advanced-degree/licensure DROP term; separately, the *title*
    must not read as senior. Seniority is checked on the title only -- a role's
    description routinely mentions a "lab manager" or "director" it reports to,
    which shouldn't disqualify an entry-level posting.
    """
    text = f"{title}\n{description}"
    return _filter.is_undergrad_accessible(
        text, require_keep=True
    ) and not _filter.has_seniority_term(title)


def _tags_from(mapping: dict, text: str) -> list[str]:
    low = text.lower()
    return [tag for tag, pats in mapping.items() if any(p in low for p in pats)]


def _apply_url(job: dict) -> str:
    opts = job.get("apply_options") or []
    if opts and opts[0].get("link"):
        return opts[0]["link"]
    return job.get("share_link") or ""


def _search(query: str, location: str) -> list[dict]:
    """Run a Google Jobs search via SerpAPI; return raw job result dicts."""
    if GoogleSearch is None:
        raise RuntimeError(
            "serpapi not installed; add 'google-search-results' to requirements"
        )
    api_key = os.environ.get(SERPAPI_KEY_ENV)
    jobs: list[dict] = []
    next_token: str | None = None
    for _ in range(MAX_PAGES_PER_QUERY):
        params = {
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "hl": "en",
            "api_key": api_key,
        }
        if next_token:
            params["next_page_token"] = next_token
        results = GoogleSearch(params).get_dict()
        if results.get("error"):
            log.warning("SerpAPI error for %r: %s", query, results["error"])
            break
        page = results.get("jobs_results") or []
        jobs.extend(page)
        next_token = (results.get("serpapi_pagination") or {}).get("next_page_token")
        if not next_token or not page:
            break
    log.info("Query %r returned %d raw result(s)", query, len(jobs))
    return jobs


def _to_listing(job: dict, tag_mapping: dict, extra_tags: list[str] | None = None) -> dict:
    title = _clean(job.get("title"))
    company = _clean(job.get("company_name"))
    location = _clean(job.get("location"))
    description = job.get("description") or ""
    detected = job.get("detected_extensions") or {}
    # Google Jobs gives an authoritative schedule_type (Full-time/Internship/...).
    emp = _employment.classify(
        title, schedule_type=detected.get("schedule_type"), text=description
    )
    tags = [emp] if emp else []
    for t in _tags_from(tag_mapping, f"{title} {description}") + (extra_tags or []):
        if t not in tags:
            tags.append(t)
    return {
        "id": _make_id(title, company),
        "title": title,
        "company": company,
        "location": location,
        "url": _apply_url(job),
        "posted_date": _parse_posted_date(detected),
        "scraped_date": _today_iso(),
        "active": True,
        "tags": tags,
        "_description": description,  # transient: consumed by the location gate
    }


def _run(queries, tag_mapping, *, undergrad_filter: bool, location: str) -> list[dict]:
    if not os.environ.get(SERPAPI_KEY_ENV):
        log.warning("%s not set; skipping SerpAPI queries", SERPAPI_KEY_ENV)
        return []

    by_id: dict[str, dict] = {}
    for query in queries:
        try:
            raw = _search(query, location)
        except Exception:  # noqa: BLE001 - isolate a single failing query
            log.exception("SerpAPI query failed: %r", query)
            continue
        for job in raw:
            title = _clean(job.get("title"))
            description = job.get("description") or ""
            if undergrad_filter and not _is_undergrad_accessible(title, description):
                continue
            listing = _to_listing(job, tag_mapping)
            by_id.setdefault(listing["id"], listing)
    return list(by_id.values())


def scrape_psychology(location: str = DEFAULT_LOCATION) -> list[dict]:
    """Undergrad-accessible psychology roles via Google Jobs."""
    listings = _run(
        PSYCH_QUERIES, PSYCH_ROLE_TAGS, undergrad_filter=True, location=location
    )
    log.info("Psychology: %d undergrad-accessible listing(s)", len(listings))
    return listings


def scrape_architecture(location: str = DEFAULT_LOCATION) -> list[dict]:
    """Architecture internship/co-op roles via Google Jobs (supplements Archinect)."""
    listings = _run(
        ARCH_QUERIES, ARCH_SOFTWARE_TAGS, undergrad_filter=True, location=location
    )
    log.info("Architecture: %d listing(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape_psychology(), indent=2, ensure_ascii=False))
