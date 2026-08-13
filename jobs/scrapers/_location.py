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

# A structured location this vague tells us nothing -> worth mining the
# description / asking the LLM. Anything more specific that still didn't match a
# target is trusted as non-target and dropped without an LLM call.
_VAGUE_LOCATIONS = {"", "united states", "usa", "us", "u.s.", "u.s.a.", "n/a",
                    "na", "various", "various locations", "multiple",
                    "multiple locations", "nationwide"}
# Target-capable states named on their own are ambiguous for our *city* targets
# (Texas -> Austin/Dallas/Houston?, Washington -> Seattle?), so they, too, get
# the description/LLM pass. (California is already a positive match; New York is
# already read as NYC by target_label.)
_AMBIGUOUS_STATES = {"texas", "washington"}
_N_LOCATIONS_RE = re.compile(r"\d+\s+locations?")


def _is_vague(location: str | None) -> bool:
    loc = (location or "").strip().lower()
    return (
        loc in _VAGUE_LOCATIONS
        or loc in _AMBIGUOUS_STATES
        or bool(_N_LOCATIONS_RE.fullmatch(loc))
    )


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


# --- Gemini fallback (batched) ---------------------------------------------
# One request resolves many listings: the throttled, rate-limited LLM is the
# bottleneck, so batching is what keeps a run (dozens of vague locations) fast.
_last_call = 0.0
_batches_made = 0
_cache: dict[str, str] = {}   # description snippet -> raw model answer
BATCH_SIZE = 15
_SNIPPET_LEN = 400

_BATCH_INSTRUCTION = (
    "You extract the primary US work location of each job posting below. For every "
    "numbered item, reply on its own line as 'N. City, ST' (two-letter US state "
    "code), or 'N. Remote' if fully remote, or 'N. Unknown' if not stated. Output "
    "only those numbered lines.\n\n"
)


def _gemini_raw_batch(texts: list[str]) -> dict[int, str]:
    """One Gemini call for up to BATCH_SIZE texts. Returns {local_index: answer}."""
    global _last_call, _batches_made
    key = os.environ.get(GEMINI_KEY_ENV)
    if not key or not texts or _batches_made >= GEMINI_MAX_CALLS:
        return {}
    prompt = _BATCH_INSTRUCTION + "\n".join(
        f"{i + 1}. {(t or '')[:_SNIPPET_LEN]}" for i, t in enumerate(texts)
    )
    wait = GEMINI_MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 16 * len(texts) + 32},
            },
            timeout=GEMINI_TIMEOUT,
        )
        _last_call = time.monotonic()
        _batches_made += 1
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:  # noqa: BLE001 - LLM is best-effort; never fatal
        # Redact the api key, which appears in the request URL of HTTP errors.
        log.warning("Gemini batch lookup failed: %s", str(exc).replace(key, "***"))
        return {}
    answers: dict[int, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
        if m:
            answers[int(m.group(1)) - 1] = m.group(2).strip()
    return answers


def gemini_batch(texts: list[str]) -> list[str | None]:
    """Resolve description texts to target labels (or None) via batched Gemini."""
    results: list[str | None] = [None] * len(texts)
    todo: list[int] = []
    for i, t in enumerate(texts):
        snippet = (t or "")[:_SNIPPET_LEN]
        if snippet in _cache:
            results[i] = target_label(_cache[snippet])
        elif snippet:
            todo.append(i)
    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start:start + BATCH_SIZE]
        answers = _gemini_raw_batch([texts[i] for i in chunk])
        for local_i, global_i in enumerate(chunk):
            ans = answers.get(local_i, "")
            _cache[(texts[global_i] or "")[:_SNIPPET_LEN]] = ans
            results[global_i] = target_label(ans)
    return results


# --- Public API ------------------------------------------------------------
def classify_rules(
    location: str | None, description: str = ""
) -> tuple[str, str | None]:
    """Rules-only classification (no network).

    Returns ``(decision, display)`` where ``decision`` is ``"keep"``, ``"drop"``,
    or ``"llm"`` (undecided -- needs a description-based LLM lookup). ``display``
    is a label to overwrite the listing's location with, or None to keep it.
    """
    label = target_label(location)
    if label:
        return "keep", ("Remote" if label == "Remote" else None)
    # A specific structured location that didn't match a target is trusted as
    # non-target and dropped now -- no description scan, no LLM.
    if _is_nontarget_state(location) or not _is_vague(location):
        return "drop", None
    label = _from_description(description)
    if label:
        return "keep", label
    if not description:
        return "drop", None
    return "llm", None


def resolve(
    location: str | None, description: str = "", *, use_llm: bool = True
) -> tuple[bool, str | None]:
    """Single-listing convenience (rules + one LLM lookup).

    Prefer ``classify_rules`` + ``gemini_batch`` for bulk work (the pipeline does),
    which batches the LLM calls. Returns ``(allowed, display)``.
    """
    decision, display = classify_rules(location, description)
    if decision == "keep":
        return True, display
    if decision == "drop":
        return False, None
    if use_llm:
        label = gemini_batch([description])[0]
        if label:
            return True, label
    return False, None
