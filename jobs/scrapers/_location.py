"""Location gate: keep only roles in the target markets.

Targets: anywhere in **California**, plus **Seattle**, **New York City**,
**Austin**, **Dallas**, **Houston**. Remote/"Anywhere" roles are kept (an
undergrad in a target market can take them); anything else -- including roles
whose location can't be determined ("United States", "2 Locations") -- is dropped.

Two-stage resolution per listing:
  1. Rules on the structured ``location`` string (fast, deterministic, free).
  2. If that's inconclusive, mine the job ``description`` -- first a "City, ST"
     regex, then (optionally) a Gemini LLM call for messy phrasing ("our Puget
     Sound studio"). Gemini needs ``GEMINI_API_KEY``; without it, step 2's LLM is
     skipped and the listing simply falls through to "unknown" -> dropped.

``resolve(location, description)`` returns ``(allowed, display)``: ``display`` is
a cleaned label when the match came from text/LLM (so the board shows something
better than "United States"), or ``None`` to keep the original string.
"""

from __future__ import annotations

import logging
import os
import re
import time

import requests

log = logging.getLogger("scrapers.location")

GEMINI_KEY_ENV = "GEMINI_API_KEY"
# The "-latest" alias tracks the current cheap/fast Flash-Lite model, so it won't
# break as dated model ids (gemini-2.0-flash, gemini-2.5-flash-lite, ...) retire.
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
GEMINI_TIMEOUT = 20
GEMINI_MIN_INTERVAL = 4.0   # ~15 requests/min, the free-tier ceiling
GEMINI_MAX_CALLS = 60       # hard cap on LLM calls per pipeline run

# --- Target matchers -------------------------------------------------------
REMOTE_TERMS = ("remote", "anywhere", "work from home", "work-from-home",
                "telecommute", "virtual")

# California is a whole-state target: the state name/abbr or any major CA city.
_CA_CITIES = (
    "los angeles", "san francisco", "san diego", "san jose", "sacramento",
    "oakland", "berkeley", "pasadena", "irvine", "santa monica", "long beach",
    "fresno", "davis", "palo alto", "mountain view", "santa clara", "sunnyvale",
    "anaheim", "la jolla", "riverside", "stanford", "culver city", "burbank",
    "glendale", "santa barbara", "santa cruz", "menlo park", "cupertino",
    "san mateo", "redwood city", "torrance", "hollywood", "malibu", "carlsbad",
    "walnut creek", "san bruno", "emeryville", "el segundo", "costa mesa",
)
# City targets -> canonical label. Guard the Texas trio against same-named towns
# in other states by rejecting an explicit non-Texas state next to the city.
_NON_TX = ("mn", "minnesota", "ga", "georgia", "or", "oregon", "pa",
           "pennsylvania", "mo", "missouri", "ms", "mississippi", "al",
           "alabama", "oh", "ohio", "nc", "north carolina")


def _has_state(text: str, abbr: str, name: str) -> bool:
    return bool(re.search(rf",\s*{abbr}\b", text)) or name in text


def _texas_city(text: str, city: str) -> bool:
    if city not in text:
        return False
    # If a state is named right after the city, it must be Texas.
    m = re.search(rf"{re.escape(city)}\s*,\s*([a-z.]+)", text)
    if m:
        st = m.group(1).strip(". ")
        if st in _NON_TX:
            return False
    return True


def target_label(text: str | None) -> str | None:
    """Return the target-market label for a location-ish string, or None."""
    if not text:
        return None
    t = f" {text.lower()} "
    if any(term in t for term in REMOTE_TERMS):
        return "Remote"
    if "california" in t or re.search(r",\s*ca\b", t) or re.search(r"\bca,\s*us", t) \
            or any(c in t for c in _CA_CITIES):
        return "California"
    if "seattle" in t:
        return "Seattle"
    if ("new york city" in t or "nyc" in t or "manhattan" in t or "brooklyn" in t
            or "new york" in t):
        return "New York City"
    if _texas_city(t, "austin"):
        return "Austin"
    if _texas_city(t, "dallas"):
        return "Dallas"
    if _texas_city(t, "houston"):
        return "Houston"
    return None


# US states as {full name: abbr}. A location naming any state OTHER than these
# target-capable ones -- CA (a target) and TX/WA/NY (may hold a target city) --
# is a definite non-target and can be dropped without touching the description or
# the LLM. (CA and NYC are already caught positively by ``target_label``.)
_TARGET_STATES = {"ca", "tx", "wa", "ny"}
_US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}
_STATE_ABBRS = set(_US_STATES.values())


def _is_nontarget_state(location: str | None) -> bool:
    """True if the location names a US state that can't contain a target market."""
    if not location:
        return False
    t = f" {location.lower()} "
    for m in re.finditer(r",\s*([a-z]{2})\b", t):
        ab = m.group(1)
        if ab in _STATE_ABBRS and ab not in _TARGET_STATES:
            return True
    for name, ab in _US_STATES.items():
        if ab not in _TARGET_STATES and f" {name} " in t:
            return True
    return False


# "City, ST" occurrences in free text (for description mining).
_CITY_STATE_RE = re.compile(r"[A-Z][a-zA-Z.'\-]+(?:\s[A-Z][a-zA-Z.'\-]+)*,\s*[A-Z]{2}\b")


def _from_description(description: str) -> str | None:
    """Precise-only location mining from prose.

    Deliberately conservative to avoid false positives: only an explicit
    "City, ST" or the whole word "california" (a whole-state target --
    unambiguous) count. Remote is intentionally NOT inferred from prose (job
    descriptions routinely mention "remote/hybrid flexibility" for on-site
    roles); remote is honored only from the structured location field. Everything
    fuzzier is left to the Gemini fallback.
    """
    if not description:
        return None
    low = f" {description.lower()} "
    if re.search(r"\bcalifornia\b", low):
        return "California"
    for m in _CITY_STATE_RE.finditer(description):
        label = target_label(m.group(0))
        if label:
            return label
    return None


# --- Gemini fallback -------------------------------------------------------
_last_call = 0.0
_calls_made = 0
_cache: dict[str, str] = {}

_PROMPT = (
    "You extract the work location of a job posting. Read the text and reply with "
    "EXACTLY ONE line and nothing else: either 'City, ST' using the two-letter US "
    "state code, or 'Remote' if it is fully remote, or 'Unknown' if the location "
    "is not stated. Text:\n\n"
)


def _gemini_location(text: str) -> str:
    """Ask Gemini for a job's location. Returns "" if unavailable/skipped."""
    global _last_call, _calls_made
    key = os.environ.get(GEMINI_KEY_ENV)
    if not key or not text:
        return ""
    if _calls_made >= GEMINI_MAX_CALLS:
        return ""
    snippet = text[:1500]
    if snippet in _cache:
        return _cache[snippet]

    wait = GEMINI_MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": key},
            json={
                "contents": [{"parts": [{"text": _PROMPT + snippet}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 20},
            },
            timeout=GEMINI_TIMEOUT,
        )
        _last_call = time.monotonic()
        _calls_made += 1
        resp.raise_for_status()
        data = resp.json()
        out = (
            data["candidates"][0]["content"]["parts"][0]["text"]
        ).strip().splitlines()[0].strip()
    except Exception as exc:  # noqa: BLE001 - LLM is best-effort; never fatal
        # Redact the api key, which appears in the request URL of HTTP errors.
        msg = str(exc).replace(key, "***")
        log.warning("Gemini location lookup failed: %s", msg)
        return ""
    _cache[snippet] = out
    return out


# --- Public API ------------------------------------------------------------
def resolve(
    location: str | None, description: str = "", *, use_llm: bool = True
) -> tuple[bool, str | None]:
    """Decide whether a listing is in a target market.

    Returns ``(allowed, display)``. ``display`` is a cleaned label to overwrite
    the listing's location with (when resolved from text/LLM), or ``None`` to
    keep the original string.
    """
    label = target_label(location)
    if label:
        # Structured field already good; only relabel the vague "remote-ish" ones.
        return True, ("Remote" if label == "Remote" else None)

    # Definite non-target state -> drop now, without spending a description scan
    # or (throttled, quota-limited) LLM call.
    if _is_nontarget_state(location):
        return False, None

    label = _from_description(description)
    if label:
        return True, label

    # LLM only for sources that actually carry a role-specific description (firm
    # ATS boards don't, and their boilerplate would mislead it).
    if use_llm and description:
        label = target_label(_gemini_location(description))
        if label:
            return True, label

    return False, None
