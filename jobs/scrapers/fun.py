"""Scraper for the FUN (Faculty for Undergraduate Neuroscience) summer list.

``funfaculty.org/undergrad_internships`` is a curated list of summer neuroscience
research programs for undergraduates -- paid research experiences that fit this
board's audience (neuroscience is psychology-adjacent, so it feeds the psychology
track). Each entry is an external link whose text is "Institution: Program".

This is a low-frequency source: the list changes about once a term, so it runs on
a ``semester`` cadence (see pipeline.Source). The programs have no structured
location, so the institution is pre-resolved with the shared matcher when the
rules can (e.g. "... Seattle" -> Seattle) and otherwise left to the pipeline's
location gate, which reads the institution name via the LLM (Columbia -> NYC).

Server-rendered but fetched via ``_http`` (curl_cffi) for consistency. Errors
raise to the caller (the pipeline isolates them).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import _filter, _http, _location

log = logging.getLogger("scrapers.fun")

PAGE_URL = "https://www.funfaculty.org/undergrad_internships"
REQUEST_TIMEOUT = 30

# Links to these hosts are FUN's own navigation / social, not programs.
_SKIP_HOSTS = (
    "funfaculty.org", "membershipsoftware.org", "facebook.com", "twitter.com",
    "x.com", "linkedin.com", "instagram.com", "youtube.com",
)


def _make_id(title: str, company: str) -> str:
    return hashlib.sha256(f"{title}{company}".encode("utf-8")).hexdigest()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _split(text: str) -> tuple[str, str]:
    """"Institution: Program" -> (title, company). Falls back to a generic title."""
    if ":" in text:
        company, title = text.split(":", 1)
        return _clean(title) or "Summer Research Program", _clean(company)
    return "Summer Research Program", _clean(text)


def _is_program_link(anchor) -> bool:
    href = anchor.get("href", "")
    if not href.startswith("http") or len(_clean(anchor.get_text())) < 6:
        return False
    host = urlparse(href).netloc.lower()
    return not any(skip in host for skip in _SKIP_HOSTS)


def scrape() -> list[dict]:
    """Scrape FUN's summer neuroscience program list. Returns listing dicts."""
    resp = _http.get(PAGE_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    by_id: dict[str, dict] = {}
    for anchor in soup.find_all("a", href=True):
        if not _is_program_link(anchor):
            continue
        text = _clean(anchor.get_text(" "))
        if not _filter.is_undergrad_accessible(text, require_keep=False):
            continue
        title, company = _split(text)
        listing = {
            "id": _make_id(title, company),
            "title": title,
            "company": company,
            # Pre-resolve an obvious target from the text; else let the gate mine it.
            "location": _location.target_label(text) or "",
            "url": anchor["href"],
            # No post date; empty pairs with the semester cadence so the listing
            # persists until the next term's run refreshes the list.
            "posted_date": "",
            "scraped_date": _today_iso(),
            "active": True,
            "tags": ["Internship", "Research"],
            "_description": text,  # transient: consumed by the location gate
        }
        by_id.setdefault(listing["id"], listing)

    listings = list(by_id.values())
    log.info("FUN: %d summer program(s)", len(listings))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape(), indent=2, ensure_ascii=False))
