"""Architecture firm careers boards, via reusable ATS adapters.

Most large architecture firms run their careers page on a hosted ATS with a
public/JSON API (Greenhouse, Workday, ...). Rather than a scraper per firm, each
firm is one entry in ``FIRMS`` naming its ATS and that ATS's parameters; a shared
adapter does the fetching (see ``_greenhouse.py`` / ``_workday.py``).

``scrape_architecture()`` is the pipeline entry point: it dispatches every firm to
its adapter, dedups across firms, and returns listing dicts. The adapters already
apply the undergrad filter (broad source: ``require_keep=True`` + seniority
screen), so this stays a thin dispatcher.

Adding a firm:
  - Greenhouse: find the board slug (the ``/{slug}/`` in its Greenhouse URL).
  - Workday:    read host/tenant/site from the careers URL
                ``https://{host}/en-US/{site}`` and the ``/cxs/{tenant}/{site}/``
                XHR path (visible in the network tab).
  - Jobvite:    the slug in ``jobs.jobvite.com/{slug}/search``.
  - UltiPro:    host/code/board_id from ``https://{host}/{code}/JobBoard/{id}/``.

Other firms live outside this file:
  - HDR (Taleo, JS-rendered) -> its own ``scrapers/hdr.py`` (headless browser).

Blocked entirely (active anti-bot; even headless Chromium is served a challenge,
so no Playwright workaround) -- do NOT retry without a CAPTCHA-solving service:
  - Corgan (iCIMS): an interactive "Human Verification" CAPTCHA puzzle.
  - AIA career center: a Cloudflare "One moment..." interstitial.
"""

from __future__ import annotations

import logging

from . import _greenhouse, _jobvite, _ultipro, _workday

log = logging.getLogger("scrapers.firms")

# Design software worth tagging (mirrors archinect.py / indeed.py).
ARCH_SOFTWARE_TAGS: dict[str, tuple[str, ...]] = {
    "Revit": ("revit",),
    "Rhino": ("rhino", "rhinoceros"),
    "AutoCAD": ("autocad", "auto cad"),
    "SketchUp": ("sketchup", "sketch up"),
    "Grasshopper": ("grasshopper",),
}

# Each firm: display name, ATS, and that ATS's parameters. ``track`` is kept for
# future non-architecture firms; ``scrape_architecture`` filters on it.
FIRMS: list[dict] = [
    {
        "name": "DLR Group",
        "track": "architecture",
        "ats": "greenhouse",
        # Interns here are seasonal (roughly Nov-Jan); 0 intern results
        # off-season is expected, not a bug.
        "params": {"slug": "dlrgroup"},
    },
    {
        "name": "CannonDesign",
        "track": "architecture",
        "ats": "greenhouse",
        "params": {"slug": "cannondesign"},
    },
    {
        "name": "Gensler",
        "track": "architecture",
        "ats": "workday",
        "params": {
            "host": "gensler.wd1.myworkdayjobs.com",
            "tenant": "gensler",
            "site": "genslercareers",
        },
    },
    {
        "name": "HKS",
        "track": "architecture",
        "ats": "workday",
        # Note: HKS serves this board's API/URLs from wd501, not the wd5 host
        # shown in its marketing links (wd5 returns 500 for job pages).
        "params": {
            "host": "hksinc.wd501.myworkdayjobs.com",
            "tenant": "hksinc",
            "site": "HKSCareers",
        },
    },
    {
        "name": "KPF",
        "track": "architecture",
        "ats": "workday",
        "params": {
            "host": "kpf.wd5.myworkdayjobs.com",
            "tenant": "kpf",
            "site": "KPF_Careers",
        },
    },
    {
        "name": "SOM",
        "track": "architecture",
        "ats": "workday",
        "params": {
            "host": "som.wd5.myworkdayjobs.com",
            "tenant": "som",
            "site": "External",
        },
    },
    {
        "name": "NBBJ",
        "track": "architecture",
        "ats": "jobvite",
        "params": {"slug": "nbbj"},
    },
    {
        "name": "Perkins&Will",
        "track": "architecture",
        "ats": "ultipro",
        "params": {
            "host": "recruiting2.ultipro.com",
            "code": "PER1007PWILL",
            "board_id": "0ca393a4-bf82-4db6-acae-91e6a0315a4a",
        },
    },
]


def _dispatch(firm: dict) -> list[dict]:
    ats = firm["ats"]
    params = firm["params"]
    name = firm["name"]
    if ats == "greenhouse":
        return _greenhouse.fetch_board(
            params["slug"], company=name, require_keep=True,
            tag_mapping=ARCH_SOFTWARE_TAGS,
        )
    if ats == "workday":
        return _workday.fetch_board(
            params["host"], params["tenant"], params["site"], company=name,
            require_keep=True, tag_mapping=ARCH_SOFTWARE_TAGS,
        )
    if ats == "jobvite":
        return _jobvite.fetch_board(
            params["slug"], company=name, require_keep=True,
            tag_mapping=ARCH_SOFTWARE_TAGS,
        )
    if ats == "ultipro":
        return _ultipro.fetch_board(
            params["code"], params["board_id"], company=name,
            host=params.get("host", "recruiting2.ultipro.com"),
            require_keep=True, tag_mapping=ARCH_SOFTWARE_TAGS,
        )
    raise ValueError(f"{name}: unknown ATS {ats!r}")


def scrape_architecture() -> list[dict]:
    """Dispatch every architecture firm to its ATS adapter; dedup; return listings."""
    by_id: dict[str, dict] = {}
    for firm in FIRMS:
        if firm.get("track", "architecture") != "architecture":
            continue
        try:
            results = _dispatch(firm)
        except Exception:  # noqa: BLE001 - one firm failing must not stop the rest
            log.exception("Firm %s failed; skipping", firm["name"])
            continue
        log.info("Firm %s: %d listing(s)", firm["name"], len(results))
        for item in results:
            by_id.setdefault(item["id"], item)
    listings = list(by_id.values())
    log.info("Firms total: %d listing(s) across %d firm(s)", len(listings), len(FIRMS))
    return listings


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json

    print(json.dumps(scrape_architecture(), indent=2, ensure_ascii=False))
