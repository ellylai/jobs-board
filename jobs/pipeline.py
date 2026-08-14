"""Job board pipeline.

Runs each scraper, merges results into the per-track JSON files, ages out
stale listings, and rewrites README.md from the JSON (the JSON is the source
of truth; the README is a generated artifact).

Design notes:
- Each scraper is independent and importable. A scraper that raises is logged
  and skipped -- one broken site never crashes the whole run.
- Only ``archinect`` is wired in for now. The others are placeholders until
  their scraper modules are implemented (see SCRAPERS below).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

# Load a local .env (SERPAPI_KEY, GEMINI_API_KEY) before anything reads the
# environment. ``usecwd=True`` walks up from the working dir, so running from
# jobs/ still finds the repo-root .env. Absent in CI (real env vars) -> no-op.
try:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
except ImportError:  # python-dotenv not installed; rely on real env vars
    pass

# --- Scrapers -------------------------------------------------------------
# Each entry maps a data file (track) to the list of scraper callables that
# feed it. A scraper is a zero-arg function returning a list of listing dicts.
from scrapers import (
    _filter, _location, archinect, dezeen, duke, firms, fun, harvard, hdr,
    indeed, wordpress_psych,
)

# Psychology-track sourcing history:
# - apa (APA PsycCareers): dropped -- APA closed the PsycCareers job board on
#   2026-07-31 with no replacement.
# - appic (scrapers/appic.py): built and working, but PARKED -- APPIC lists
#   doctoral-capstone internships (require a master's, passed comps, an approved
#   dissertation proposal). This board targets undergrads, so APPIC's audience
#   doesn't fit. Kept in the repo in case a doctoral board is ever wanted.
# - society boards (SPSP, APS, SRCD, SAS): evaluated and SKIPPED -- they're
#   senior-academic recruitment (tenure-track/professor/postdoc). Every listing
#   trips the seniority/advanced-degree filter, so undergrad yield is ~0 (SRCD is
#   also a JS-rendered shell). Not worth a scraper.
# - summer programs (Phase 3b): FUN's list is scraped (see the 'fun' source).
#   NSF REU was deferred (redesigned to a JS/challenge app with no reachable API);
#   NIH SIP (Bethesda MD) and Yale Child Study (New Haven CT) were skipped -- each
#   is a single non-target location, so the location gate drops them outright.
# The psychology track targets undergrad-accessible roles (research assistant,
# lab intern, clinical aide, etc.) via curated boards (Duke, Harvard's post-grad
# research jobs, FUN's summer programs, the psychology jobs/internships blog)
# plus SerpAPI/Google Jobs.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pipeline")

# --- Paths ----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent   # jobs/
ROOT_DIR = BASE_DIR.parent                   # repo root
DATA_DIR = BASE_DIR / "data"
# Generated markdown lives at the repo root so the track files render as
# clickable tabs on the GitHub landing page. JSON stays the source of truth.
README_PATH = ROOT_DIR / "README.md"

# --- Config ---------------------------------------------------------------
MAX_AGE_DAYS = 60          # listings older than this are dropped entirely
NEW_BADGE_HOURS = 48       # listings newer than this get a 🆕 prefix

# Target markets: anywhere in California + these cities (plus remote). Enforced
# by the location gate below; everything else -- including undeterminable
# locations -- is dropped. See scrapers/_location.py.

@dataclass(frozen=True)
class Source:
    """One scraper plus how often to run it.

    ``frequency`` is one of ``daily`` / ``weekly`` / ``monthly`` / ``semester``.
    Low-frequency sources (e.g. summer programs that post once a term) don't run
    every day; the merge keeps their listings active in between (see ``merge``),
    so they don't flicker off the board on the days they're skipped.
    """
    name: str
    scrape: Callable[[], list[dict]]
    frequency: str = "daily"


# Track -> the sources feeding it. Free/curated sources first; the SerpAPI web
# search (quota-limited, may return []) runs last.
SCRAPERS: dict[str, list[Source]] = {
    "architecture": [
        Source("archinect", archinect.scrape),
        Source("dezeen", dezeen.scrape),
        Source("hdr", hdr.scrape),
        Source("firms", firms.scrape_architecture),
        # indeed.scrape_architecture is available but disabled to conserve the
        # SerpAPI free-tier quota -- Archinect + firm boards cover architecture.
    ],
    "psychology": [
        Source("wordpress_psych", wordpress_psych.scrape),
        Source("duke", duke.scrape),
        Source("harvard", harvard.scrape),
        # Summer research programs change ~once a term -> run twice a year (the
        # source-aware merge keeps its listings active in between).
        Source("fun", fun.scrape, frequency="semester"),
        Source("indeed", indeed.scrape_psychology),
    ],
}

# Set RUN_ALL_SOURCES=1 to ignore cadence and run every source (manual /
# workflow_dispatch runs and local testing).
FORCE_ALL_ENV = "RUN_ALL_SOURCES"


# --- Time helpers ---------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_iso() -> str:
    return _now().date().isoformat()


def _parse_date(value: str | None) -> datetime | None:
    """Parse an ISO date/datetime string to an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # Fall back to date-only.
        try:
            dt = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- JSON I/O -------------------------------------------------------------
def load_listings(track: str) -> list[dict]:
    path = DATA_DIR / f"{track}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        log.error("Corrupt JSON in %s; starting from empty list", path.name)
        return []


def save_listings(track: str, listings: list[dict]) -> None:
    path = DATA_DIR / f"{track}.json"
    path.write_text(
        json.dumps(listings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --- Scraper runner -------------------------------------------------------
def is_due(frequency: str, today: date, *, force: bool = False) -> bool:
    """Whether a source of this frequency should run on ``today``."""
    if force or frequency == "daily":
        return True
    if frequency == "weekly":
        return today.weekday() == 0            # Monday
    if frequency == "monthly":
        return today.day == 1
    if frequency == "semester":
        return today.day == 1 and today.month in (1, 8)  # start of spring / fall
    log.warning("Unknown frequency %r; treating as daily", frequency)
    return True


def run_scrapers(
    sources: list[Source], today: date, *, force: bool = False
) -> tuple[list[dict], set[str]]:
    """Run the sources that are due today, isolating failures.

    Returns ``(listings, ran)`` where ``ran`` is the set of source names that ran
    *successfully* -- a source that was skipped (not due) or raised is absent, so
    the merge won't deactivate its existing listings.
    """
    collected: list[dict] = []
    ran: set[str] = set()
    for src in sources:
        if not is_due(src.frequency, today, force=force):
            log.info("Skipping %s (%s; not due today)", src.name, src.frequency)
            continue
        try:
            results = src.scrape() or []
        except Exception:  # noqa: BLE001 - one bad scraper must not stop the run
            log.exception("Scraper %s failed; keeping its prior listings", src.name)
            continue
        ran.add(src.name)
        for item in results:
            item["source"] = src.name
        log.info("%s returned %d listing(s)", src.name, len(results))
        collected.extend(results)
    return collected, ran


# --- Location gate --------------------------------------------------------
def apply_location_gate(listings: list[dict]) -> list[dict]:
    """Keep only listings in a target market; strip the transient _description.

    Two passes so the LLM is batched: first classify every listing by rules,
    collecting the undecided ones; then resolve those together with one batched
    Gemini call per group. Scrapers attach ``_description`` (a stripped body) for
    sources whose structured location is vague; it's removed before returning so
    it never reaches the JSON.
    """
    decisions: list[list] = []
    pending_idx: list[int] = []
    pending_txt: list[str] = []
    for i, item in enumerate(listings):
        description = item.get("_description", "")
        decision, display = _location.classify_rules(item.get("location", ""), description)
        decisions.append([decision, display])
        if decision == "llm":
            pending_idx.append(i)
            pending_txt.append(description)

    if pending_idx:
        labels = _location.gemini_batch(pending_txt)
        for k, i in enumerate(pending_idx):
            label = labels[k]
            decisions[i] = ["keep" if label else "drop", label]

    kept: list[dict] = []
    for item, (decision, display) in zip(listings, decisions):
        item.pop("_description", None)
        if decision != "keep":
            continue
        if display:
            item["location"] = display
        kept.append(item)
    return kept


# --- Merge ----------------------------------------------------------------
def merge(existing: list[dict], scraped: list[dict], ran_sources: set[str]) -> list[dict]:
    """Merge freshly scraped listings into existing ones.

    - Dedup on ``id``.
    - Listings seen this run are marked active.
    - An existing listing not seen this run is marked inactive ONLY if its source
      ran this cycle (so a low-frequency source that was skipped, or one that
      failed transiently, keeps its listings active). Legacy listings with no
      recorded source are treated as belonging to a source that ran.
    - Listings older than MAX_AGE_DAYS (by posted_date) are dropped entirely.
    """
    today = _today_iso()
    by_id: dict[str, dict] = {item["id"]: item for item in existing if item.get("id")}
    seen_ids: set[str] = set()

    for item in scraped:
        lid = item.get("id")
        if not lid:
            log.warning("Scraped listing missing id; skipping: %r", item.get("title"))
            continue
        seen_ids.add(lid)
        if lid in by_id:
            # Update mutable fields but preserve the original scraped_date.
            prev = by_id[lid]
            prev.update(item)
            prev["scraped_date"] = prev.get("scraped_date") or item.get("scraped_date") or today
            prev["active"] = True
        else:
            item.setdefault("scraped_date", today)
            item["active"] = True
            by_id[lid] = item

    # Deactivate unseen listings only when their source actually ran this cycle.
    for lid, item in by_id.items():
        if lid not in seen_ids and (item.get("source") in ran_sources or not item.get("source")):
            item["active"] = False

    # Drop listings older than MAX_AGE_DAYS.
    cutoff = _now().timestamp() - MAX_AGE_DAYS * 86400
    merged: list[dict] = []
    for item in by_id.values():
        posted = _parse_date(item.get("posted_date"))
        if posted is not None and posted.timestamp() < cutoff:
            continue
        merged.append(item)

    return merged


# --- README rendering -----------------------------------------------------
def _sort_key(item: dict):
    dt = _parse_date(item.get("posted_date"))
    return dt.timestamp() if dt else 0.0


def _md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _render_table(listings: list[dict]) -> str:
    header = (
        "| Company | Role | Location | Posted | Tags | Apply |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
    )
    active = [x for x in listings if x.get("active", True)]
    active.sort(key=_sort_key, reverse=True)

    if not active:
        return "_No active listings yet._\n"

    now = _now()
    rows: list[str] = []
    for item in active:
        company = _md_escape(item.get("company", ""))
        posted = _parse_date(item.get("posted_date"))
        if posted is not None and (now - posted).total_seconds() <= NEW_BADGE_HOURS * 3600:
            company = f"🆕 {company}"

        role = _md_escape(item.get("title", ""))
        location = _md_escape(item.get("location", ""))
        posted_str = posted.date().isoformat() if posted else "—"
        tags = _md_escape(", ".join(item.get("tags", []) or []))
        url = item.get("url", "")
        apply = f"[Apply]({url})" if url else "—"

        rows.append(
            f"| {company} | {role} | {location} | {posted_str} | {tags} | {apply} |"
        )

    return header + "\n".join(rows) + "\n"


# Display metadata per track. The order here is the order tracks appear.
TRACKS_META: dict[str, dict[str, str]] = {
    "architecture": {"title": "Architecture", "emoji": "🏛️"},
    "psychology": {"title": "Psychology", "emoji": "🧠"},
}

# The only hand-editable part of README.md: content between these markers is
# preserved verbatim across regenerations (write your intro there, in place).
_INTRO_START = "<!-- intro:start -->"
_INTRO_END = "<!-- intro:end -->"
_DEFAULT_INTRO = (
    "_Write your introduction here. This block is preserved when the board "
    "regenerates — everything else on this page is auto-generated._"
)
_INTRO_RE = re.compile(
    re.escape(_INTRO_START) + r"\n?(.*?)\n?" + re.escape(_INTRO_END), re.DOTALL
)

# Footer: notes the file is generated (except the intro) and points devs to the
# maintainer guide.
_FOOTER = (
    "---\n\n"
    "_made by ellyse for friends xx | "
    "for dev: see [`jobs/README.md`](jobs/README.md)._"
)

# Concise note on the two filters every listing passes (see scrapers/_filter.py
# and scrapers/_location.py).
_FILTERS_NOTE = (
    "Every listing is auto-filtered on two axes:\n\n"
    "- **Undergraduate-accessible** — interns, research assistants, and other "
    "entry-level roles are kept; anything requiring an advanced degree, licensure, "
    "or seniority (PhD, \"Senior\", \"Manager\", 3+ years, ...) is dropped.\n"
    "- **Target market** — anywhere in California, or Seattle / New York City / "
    "Austin / Dallas / Houston; fully-remote roles are also kept. Everything else "
    "is dropped.\n\n"
    "🆕 marks listings posted in the last 48 hours."
)


def _active_count(listings: list[dict]) -> int:
    return sum(1 for x in listings if x.get("active", True))


def _render_section(track: str, listings: list[dict]) -> str:
    """Render one track as a collapsible <details> block containing its table."""
    meta = TRACKS_META[track]
    count = _active_count(listings)
    # Blank lines around the table are required for GitHub to render Markdown
    # inside the <details> element.
    return (
        "<details>\n"
        f"<summary><strong>{meta['emoji']} {meta['title']}</strong> — "
        f"{count} open role(s)</summary>\n\n"
        f"{_render_table(listings)}\n"
        "</details>"
    )


def _preserved_intro() -> str:
    """Return the hand-written intro from the current README (between the markers),
    or a default placeholder if none is present yet."""
    if README_PATH.exists():
        match = _INTRO_RE.search(README_PATH.read_text(encoding="utf-8"))
        if match and match.group(1).strip():
            return match.group(1).strip()
    return _DEFAULT_INTRO


def render_readme(tracks: dict[str, list[dict]]) -> None:
    """Render the single root README.md: a preserved intro block, the filter note,
    one collapsible section per track (both tables live on the landing page), and
    a developer footer."""
    updated = _now().strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        "# Job Board",
        "",
        f"_Last updated: {updated}_",
        "",
        _INTRO_START,
        _preserved_intro(),
        _INTRO_END,
        "",
        _FILTERS_NOTE,
        "",
        "## Open roles",
        "",
    ]
    for track in TRACKS_META:
        parts.append(_render_section(track, tracks.get(track, [])))
        parts.append("")
    parts.append(_FOOTER)
    README_PATH.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    log.info("Wrote %s", README_PATH.name)


# --- Main -----------------------------------------------------------------
def main() -> None:
    today = _now().date()
    force = os.environ.get(FORCE_ALL_ENV, "").strip().lower() in ("1", "true", "yes")
    if force:
        log.info("%s set; running every source regardless of cadence", FORCE_ALL_ENV)

    tracks: dict[str, list[dict]] = {}
    for track, sources in SCRAPERS.items():
        existing = load_listings(track)
        scraped, ran = run_scrapers(sources, today, force=force)
        # Safety net: drop any listing whose title trips the advanced-degree /
        # licensure DROP screen, whatever its source claimed. DROP-only (no
        # require_keep, no seniority), so curated sources are unaffected.
        before = len(scraped)
        scraped = _filter.filter_listings(scraped, require_keep=False, text_keys=("title",))
        if len(scraped) != before:
            log.info("Safety net dropped %d listing(s) in %s", before - len(scraped), track)
        # Location gate: keep only target-market (or remote) roles.
        before = len(scraped)
        scraped = apply_location_gate(scraped)
        log.info("Location gate: kept %d of %d listing(s) in %s", len(scraped), before, track)
        merged = merge(existing, scraped, ran)
        save_listings(track, merged)
        active = sum(1 for x in merged if x.get("active"))
        log.info("Track %s: %d total, %d active", track, len(merged), active)
        tracks[track] = merged

    render_readme(tracks)


if __name__ == "__main__":
    main()
