"""Shared undergrad-accessibility filter.

This job board targets undergraduates, so every listing from every source is run
through one filter before it is stored. Centralising it here keeps the policy
consistent (and easy to tune) instead of copy-pasted across scrapers.

Policy:
- DROP wins: if the text names an advanced-degree / licensure requirement, the
  listing is rejected outright. This applies to every source.
- SENIORITY is an *opt-in* second drop screen (``drop_seniority=True``), matched
  on word boundaries. It exists for broad sources that filter on job *titles*
  (firm ATS boards, web search): a "Senior/Manager/Principal/Faculty" title is
  not undergrad-accessible. Curated sources leave it off, because those same
  words appear innocuously in research-posting descriptions ("faculty mentor",
  "principal investigator") that we do want to keep.
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

# Entry-level / undergrad markers -> positive signal. ``designer`` and ``job
# captain`` are the early-career architecture titles (a plain "Architect" needs
# licensure); harmless to the psychology track, where they don't occur.
KEEP_TERMS = (
    "undergraduate", "undergrad", "bachelor", "post-bac", "post-baccalaureate",
    "postbac", "recent graduate", "pre-grad", "pre-doctoral", "predoctoral",
    "entry-level", "entry level", "no experience required", "intern",
    "internship", "co-op", "coop", "research assistant", "research coordinator",
    "volunteer", "aide", "designer", "job captain",
)

# Seniority markers -> opt-in drop screen for title-based broad sources. Matched
# on word boundaries so "lead" doesn't hit "leadership", "sr" doesn't hit
# "disrupt", etc. ``intermediate`` is included: it denotes several years'
# experience, above an undergrad's reach.
SENIORITY_TERMS = (
    "senior", "sr", "director", "manager", "supervisor", "principal",
    "vice president", "vp", "head of", "chief", "lead", "faculty", "professor",
    "attending", "intermediate", "dean",
)

# Minimum years-of-experience that puts a role beyond undergrad reach. A stated
# range starting at or above this (e.g. "5-9 Years") is dropped; "0-4 Years" is
# kept (its minimum is 0).
SENIORITY_MIN_YEARS = 3

_DROP_RE = re.compile("|".join(re.escape(t) for t in DROP_TERMS), re.IGNORECASE)
_KEEP_RE = re.compile("|".join(re.escape(t) for t in KEEP_TERMS), re.IGNORECASE)
_SENIORITY_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(t) for t in SENIORITY_TERMS), re.IGNORECASE
)
# Level suffixes II..VI (roman) mark experienced grades; level I / "IB" are
# entry and deliberately excluded.
_LEVEL_RE = re.compile(r"\b(?:ii|iii|iv|v|vi)\b", re.IGNORECASE)
# The first number preceding "year(s)", e.g. the 5 in "5-9 Years" or "5+ years".
_YEARS_RE = re.compile(r"(\d+)\s*(?:-\s*\d+\s*|\+\s*)?years?", re.IGNORECASE)


def has_drop_term(text: str) -> bool:
    return bool(_DROP_RE.search(text or ""))


def has_keep_term(text: str) -> bool:
    return bool(_KEEP_RE.search(text or ""))


def _requires_experience(text: str) -> bool:
    return any(
        int(m.group(1)) >= SENIORITY_MIN_YEARS for m in _YEARS_RE.finditer(text or "")
    )


def has_seniority_term(text: str) -> bool:
    """Seniority signal: a seniority word, a level suffix (II+), or a years-of-
    experience requirement at or above ``SENIORITY_MIN_YEARS``."""
    text = text or ""
    return bool(
        _SENIORITY_RE.search(text)
        or _LEVEL_RE.search(text)
        or _requires_experience(text)
    )


def is_undergrad_accessible(
    text: str, *, require_keep: bool = False, drop_seniority: bool = False
) -> bool:
    """Return True if ``text`` reads as an undergrad-accessible role.

    ``text`` should combine the title and any description/snippet available.
    ``drop_seniority`` adds the seniority word-boundary screen (see module docs);
    broad title-based sources enable it, curated description sources don't.
    """
    if has_drop_term(text):
        return False
    if drop_seniority and has_seniority_term(text):
        return False
    if require_keep and not has_keep_term(text):
        return False
    return True


def filter_listings(
    listings: list[dict],
    *,
    require_keep: bool = False,
    drop_seniority: bool = False,
    text_keys=("title", "description"),
) -> list[dict]:
    """Filter a list of listing dicts by undergrad-accessibility.

    Joins the given keys into the text checked. ``description`` is optional on
    listings; missing keys are simply skipped.
    """
    kept = []
    for item in listings:
        text = " ".join(str(item.get(k, "")) for k in text_keys)
        if is_undergrad_accessible(
            text, require_keep=require_keep, drop_seniority=drop_seniority
        ):
            kept.append(item)
    return kept
