"""Shared employment-type classifier.

Every listing leads with one employment-type tag (Internship / Full-Time /
Part-Time / Contract / Volunteer) so the board reads clearly. Sources vary in how
much they tell us, so ``classify`` takes whatever authoritative signal a scraper
has and falls back through weaker ones:

    schedule_type / category  (authoritative: Google Jobs, the WP blog)
      -> the job title          ("... Intern", "Volunteer ...")
      -> free text              (a stripped description)
      -> a caller-supplied default (firm ATS boards pass "Full-Time": a design
         firm's non-intern postings are salaried professional roles)

Returns "" when nothing matches and no default is given.
"""

from __future__ import annotations

# Checked in priority order: an internship that is also "full-time hours" should
# still read as Internship, etc.
_RULES = (
    ("Internship", ("intern", "co-op", "coop", "co op")),
    ("Volunteer", ("volunteer",)),
    ("Part-Time", ("part-time", "part time")),
    ("Contract", ("contract", "temporary", "seasonal", "temp ")),
    ("Full-Time", ("full-time", "full time")),
)


def _match(text: str | None) -> str:
    low = (text or "").lower()
    for label, needles in _RULES:
        if any(n in low for n in needles):
            return label
    return ""


def classify(
    title: str = "",
    *,
    schedule_type: str | None = None,
    category: str | None = None,
    text: str = "",
    default: str = "",
) -> str:
    """Return a single employment-type label (or ``default``/"" if unknown)."""
    for authoritative in (schedule_type, category):
        if authoritative:
            label = _match(authoritative)
            if label:
                return label
    return _match(title) or _match(text) or default
