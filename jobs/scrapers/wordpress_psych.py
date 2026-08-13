"""Scraper for the Psychology Job and Internship Opportunities blog.

`psychologyjobsinternships.wordpress.com` is a curated blog of psychology roles
aimed at students and recent grads -- exactly this board's audience. WordPress.com
exposes every site through the public WP REST API, so no scraping of rendered HTML
is needed:

    GET https://public-api.wordpress.com/wp/v2/sites/{site}/categories
    GET https://public-api.wordpress.com/wp/v2/sites/{site}/posts?categories=...

The blog tags each post with a job-type category (Full-Time Job, Summer/Fall-Spring
Internship, Part-Time Job, Research Assistant/Lab Manager), a US state, and one or
more psychology subfields. We fetch only the job-type categories (skipping
announcements like "Summer Break!"), derive location from the state category and
tags from the subfield categories, and split the post title into role + org.

Curated source: ``require_keep=False`` (trust the blog's own curation; only the
advanced-degree/licensure DROP screen applies). Feeds the ``psychology`` track.

Independent and importable: ``from scrapers import wordpress_psych`` then
``wordpress_psych.scrape()``. Errors raise to the caller (pipeline isolates them).
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timezone

import requests

from . import _employment, _filter

log = logging.getLogger("scrapers.wordpress_psych")

SITE = "psychologyjobsinternships.wordpress.com"
API = f"https://public-api.wordpress.com/wp/v2/sites/{SITE}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
POSTS_PER_PAGE = 50  # most-recent job posts; the pipeline ages out the stale ones

# A category is a job posting (not an announcement) if its name mentions one of
# these. Matched case-insensitively against the blog's category names.
JOB_CATEGORY_KEYWORDS = (
    "full-time job", "part-time job", "internship", "workshop",
    "research assistant", "lab manager",
)

# US states + DC (plus a few countries) used to derive a location from a post's
# state category. Lower-cased for lookup.
LOCATION_NAMES = {
    s.lower() for s in (
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
        "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
        "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
        "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
        "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
        "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
        "West Virginia", "Wisconsin", "Wyoming", "Washington DC",
        "District of Columbia", "Canada", "England", "Remote",
    )
}

# Separators that split "<role> for the <org>" / "<role> at <org>" titles. Tried
# in order; the earliest-occurring one wins so the role stays the clean job title.
_TITLE_SEPARATORS = (" for the ", " for ", " at the ", " at ")


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean(text: str | None) -> str:
    # \s matches the &nbsp; (U+00A0) these titles are littered with.
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _strip_html(content: str | None) -> str:
    if not content:
        return ""
    return _clean(re.sub(r"<[^>]+>", " ", content))


def _split_title(raw: str) -> tuple[str, str]:
    """Split a post title into (role, org). Falls back to (raw, "")."""
    best: tuple[int, str] | None = None
    for sep in _TITLE_SEPARATORS:
        i = raw.find(sep)
        if i != -1 and (best is None or i < best[0]):
            best = (i, sep)
    if best is None:
        return raw, ""
    idx, sep = best
    return raw[:idx].strip(), raw[idx + len(sep):].strip()


def _get(path: str, **params) -> requests.Response:
    resp = requests.get(
        f"{API}/{path}",
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp


def _load_categories() -> dict[int, str]:
    """Return {category_id: name} for the whole blog (81 categories, one page)."""
    cats = _get("categories", per_page=100).json()
    return {c["id"]: c["name"] for c in cats}


def _job_category_ids(id2name: dict[int, str]) -> list[int]:
    return [
        cid for cid, name in id2name.items()
        if any(k in name.lower() for k in JOB_CATEGORY_KEYWORDS)
    ]


def _location_from(cat_ids: list[int], id2name: dict[int, str]) -> str:
    for cid in cat_ids:
        name = id2name.get(cid, "")
        if name.lower() in LOCATION_NAMES:
            return name
    return ""


def _employment_of(cat_ids: list[int], id2name: dict[int, str], job_ids: set[int]) -> str:
    """Employment-type label from the post's job-type categories."""
    names = " ".join(id2name.get(cid, "") for cid in cat_ids if cid in job_ids)
    return _employment.classify(category=names)


def _subfield_tags(cat_ids: list[int], id2name: dict[int, str]) -> list[str]:
    """Psychology-subfield tags: "Clinical Psychology" -> "Clinical"."""
    tags: list[str] = []
    for cid in cat_ids:
        name = id2name.get(cid, "")
        if name.lower().endswith(" psychology") and name not in tags:
            tags.append(name[: -len(" Psychology")])
    return tags[:3]


def scrape() -> list[dict]:
    """Scrape job/internship posts from the psychology blog. Returns listings."""
    id2name = _load_categories()
    job_ids = _job_category_ids(id2name)
    if not job_ids:
        log.warning("No job-type categories found; returning nothing")
        return []
    log.info("Fetching posts in %d job-type categor(y/ies)", len(job_ids))

    posts = _get(
        "posts",
        categories=",".join(str(i) for i in job_ids),
        per_page=POSTS_PER_PAGE,
        _fields="id,date,link,title,content,categories",
    ).json()

    job_id_set = set(job_ids)
    by_id: dict[str, dict] = {}
    for post in posts:
        raw_title = _clean((post.get("title") or {}).get("rendered"))
        if not raw_title:
            continue
        body = _strip_html((post.get("content") or {}).get("rendered"))
        # Curated source: only the advanced-degree/licensure DROP screen applies.
        if not _filter.is_undergrad_accessible(f"{raw_title} {body}", require_keep=False):
            continue

        role, company = _split_title(raw_title)
        cat_ids = post.get("categories") or []
        emp = _employment_of(cat_ids, id2name, job_id_set)
        tags = ([emp] if emp else []) + _subfield_tags(cat_ids, id2name)
        listing = {
            "id": _make_id(role, company),
            "title": role,
            "company": company,
            "location": _location_from(cat_ids, id2name),
            "url": post.get("link") or "",
            "posted_date": (post.get("date") or "")[:10] or _today_iso(),
            "scraped_date": _today_iso(),
            "active": True,
            "tags": tags,
            "_description": body,  # transient: consumed by the location gate
        }
        by_id.setdefault(listing["id"], listing)

    listings = list(by_id.values())
    log.info("Psychology blog: %d listing(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
