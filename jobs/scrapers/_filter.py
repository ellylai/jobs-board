"""Shared undergrad-accessibility filter.

This job board targets undergraduates, so every listing from every source is run
through one filter before it is stored. Centralising it here keeps the policy
consistent (and easy to tune) instead of copy-pasted across scrapers.

Policy:
- DROP wins: if the text names an advanced-degree / licensure / seniority
  requirement, the listing is rejected outright.
- KEEP is a positive signal. For broad, noisy sources (web search, general job
  boards) callers pass ``require_keep=True`` so only listings that explicitly
  read as undergrad/entry-level survive. For curated undergrad sources (e.g. a
  post-bac board) callers leave it False so relevant listings aren't dropped
  merely for lacking a magic word.
"""

from __future__ import annotations

import re

# Advanced-degree / licensure / seniority markers -> not undergrad-accessible.
DROP_TERMS = (
    "phd required", "ph.d required", "ph.d. required", "doctoral required",
    "doctorate required", "md required", "postdoc", "post-doc",
    "currently enrolled in doctoral", "graduate degree required",
    "master's required", "masters required", "master’s required",
    "licensure required", "licensed ", "lcsw", "lpc", "lmft", "psyd required",
    "board certified", "bcba",
)

# Entry-level / undergrad markers -> positive signal.
KEEP_TERMS = (
    "undergraduate", "undergrad", "bachelor", "post-bac", "post-baccalaureate",
    "postbac", "recent graduate", "pre-grad", "pre-doctoral", "predoctoral",
    "entry-level", "entry level", "no experience required", "intern",
    "internship", "co-op", "coop", "research assistant", "research coordinator",
    "volunteer", "aide",
)

_DROP_RE = re.compile("|".join(re.escape(t) for t in DROP_TERMS), re.IGNORECASE)
_KEEP_RE = re.compile("|".join(re.escape(t) for t in KEEP_TERMS), re.IGNORECASE)


def has_drop_term(text: str) -> bool:
    return bool(_DROP_RE.search(text or ""))


def has_keep_term(text: str) -> bool:
    return bool(_KEEP_RE.search(text or ""))


def is_undergrad_accessible(text: str, *, require_keep: bool = False) -> bool:
    """Return True if ``text`` reads as an undergrad-accessible role.

    ``text`` should combine the title and any description/snippet available.
    """
    if has_drop_term(text):
        return False
    if require_keep and not has_keep_term(text):
        return False
    return True


def filter_listings(
    listings: list[dict], *, require_keep: bool = False, text_keys=("title", "description")
) -> list[dict]:
    """Filter a list of listing dicts by undergrad-accessibility.

    Joins the given keys into the text checked. ``description`` is optional on
    listings; missing keys are simply skipped.
    """
    kept = []
    for item in listings:
        text = " ".join(str(item.get(k, "")) for k in text_keys)
        if is_undergrad_accessible(text, require_keep=require_keep):
            kept.append(item)
    return kept
