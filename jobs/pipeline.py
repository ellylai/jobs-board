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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

# --- Scrapers -------------------------------------------------------------
# Each entry maps a data file (track) to the list of scraper callables that
# feed it. A scraper is a zero-arg function returning a list of listing dicts.
from scrapers import _filter, archinect, duke, firms, indeed, wordpress_psych

# Psychology-track sourcing history:
# - apa (APA PsycCareers): dropped -- APA closed the PsycCareers job board on
#   2026-07-31 with no replacement.
# - appic (scrapers/appic.py): built and working, but PARKED -- APPIC lists
#   doctoral-capstone internships (require a master's, passed comps, an approved
#   dissertation proposal). This board targets undergrads, so APPIC's audience
#   doesn't fit. Kept in the repo in case a doctoral board is ever wanted.
# The psychology track targets undergrad-accessible roles (research assistant,
# lab intern, clinical aide, etc.) via curated boards (Duke, the psychology
# jobs/internships blog) plus SerpAPI/Google Jobs.

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

# Track -> data file -> scrapers feeding it. Free/curated sources first; the
# SerpAPI web search (quota-limited, may return []) runs last.
SCRAPERS: dict[str, list[Callable[[], list[dict]]]] = {
    "architecture": [
        archinect.scrape,
        firms.scrape_architecture,
        # indeed.scrape_architecture is available but disabled to conserve the
        # SerpAPI free-tier quota -- Archinect + firm boards cover architecture.
    ],
    "psychology": [
        wordpress_psych.scrape,
        duke.scrape,
        indeed.scrape_psychology,
    ],
}


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
def run_scrapers(scrapers: Iterable[Callable[[], list[dict]]]) -> list[dict]:
    """Run each scraper, isolating failures. Returns all collected listings."""
    collected: list[dict] = []
    for scraper in scrapers:
        name = getattr(scraper, "__module__", "?") + "." + getattr(scraper, "__name__", "?")
        try:
            results = scraper() or []
            log.info("%s returned %d listing(s)", name, len(results))
            collected.extend(results)
        except Exception:  # noqa: BLE001 - one bad scraper must not stop the run
            log.exception("Scraper %s failed; skipping", name)
    return collected


# --- Merge ----------------------------------------------------------------
def merge(existing: list[dict], scraped: list[dict]) -> list[dict]:
    """Merge freshly scraped listings into existing ones.

    - Dedup on ``id``.
    - Listings seen this run are marked active; existing listings not seen are
      marked inactive (kept for history until they age out).
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

    # Mark listings not seen this run as inactive.
    for lid, item in by_id.items():
        if lid not in seen_ids:
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


# Display metadata + output filename per track. The order here is the order
# tracks appear on the index README.
TRACKS_META: dict[str, dict[str, str]] = {
    "architecture": {"title": "Architecture", "emoji": "🏛️", "file": "ARCHITECTURE.md"},
    "psychology": {"title": "Psychology", "emoji": "🧠", "file": "PSYCHOLOGY.md"},
}

_GENERATED_NOTE = "Auto-generated from the JSON data files by `pipeline.py`. Do not edit by hand."


def _active_count(listings: list[dict]) -> int:
    return sum(1 for x in listings if x.get("active", True))


def _render_track_file(track: str, listings: list[dict], updated: str) -> None:
    """Write a single ARCHITECTURE.md / PSYCHOLOGY.md track page."""
    meta = TRACKS_META[track]
    parts = [
        f"# {meta['emoji']} {meta['title']}",
        "",
        f"_Last updated: {updated}_ · [← All tracks](README.md)",
        "",
        _GENERATED_NOTE,
        "",
        _render_table(listings),
    ]
    path = ROOT_DIR / meta["file"]
    path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    log.info("Wrote %s", path.name)


def _render_index(tracks: dict[str, list[dict]], updated: str) -> None:
    """Write the root README.md landing page linking to each track."""
    parts = [
        "# Job Board",
        "",
        f"_Last updated: {updated}_",
        "",
        _GENERATED_NOTE,
        "",
        "## Tracks",
        "",
        "| Track | Open roles | Board |",
        "| --- | --- | --- |",
    ]
    for track, meta in TRACKS_META.items():
        count = _active_count(tracks.get(track, []))
        parts.append(
            f"| {meta['emoji']} {meta['title']} | {count} | [{meta['file']}]({meta['file']}) |"
        )
    README_PATH.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    log.info("Wrote %s", README_PATH.name)


def render_readme(tracks: dict[str, list[dict]]) -> None:
    """Render the index README plus one markdown page per track."""
    updated = _now().strftime("%Y-%m-%d %H:%M UTC")
    for track in TRACKS_META:
        _render_track_file(track, tracks.get(track, []), updated)
    _render_index(tracks, updated)


# --- Main -----------------------------------------------------------------
def main() -> None:
    tracks: dict[str, list[dict]] = {}
    for track, scrapers in SCRAPERS.items():
        existing = load_listings(track)
        scraped = run_scrapers(scrapers)
        # Safety net: drop any listing whose title trips the advanced-degree /
        # licensure DROP screen, whatever its source claimed. DROP-only (no
        # require_keep, no seniority), so curated sources are unaffected.
        before = len(scraped)
        scraped = _filter.filter_listings(scraped, require_keep=False, text_keys=("title",))
        if len(scraped) != before:
            log.info("Safety net dropped %d listing(s) in %s", before - len(scraped), track)
        merged = merge(existing, scraped)
        save_listings(track, merged)
        active = sum(1 for x in merged if x.get("active"))
        log.info("Track %s: %d total, %d active", track, len(merged), active)
        tracks[track] = merged

    render_readme(tracks)


if __name__ == "__main__":
    main()
