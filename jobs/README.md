# Job Board — maintainer guide

Developer/maintainer docs for the pipeline. The repo-root `README.md` is the
**user-facing** board and is fully generated — never edit it by hand.

## How it works

```
scrapers/*  ──▶  pipeline.py  ──▶  data/*.json (source of truth)  ──▶  ../README.md
                     │
        per-source cadence · undergrad filter · location gate · employment tags
```

A daily GitHub Action runs `pipeline.py`, which: runs each due scraper (isolating
failures), applies the filters, merges results into `data/<track>.json`, ages out
stale listings, and regenerates the root `README.md`. The JSON is the source of
truth; the README is a disposable artifact.

## Repo structure

```
jobs-board/
├── README.md                     # generated user board (do NOT edit)
├── .github/workflows/scrape.yml  # daily cron + manual dispatch
├── .env                          # local secrets (gitignored)
└── jobs/
    ├── README.md                 # this file
    ├── pipeline.py               # orchestrator: cadence, filters, merge, render
    ├── requirements.txt
    ├── data/{architecture,psychology}.json   # source of truth
    └── scrapers/
        ├── _filter.py            # undergrad-accessibility filter (KEEP/DROP/seniority)
        ├── _location.py          # target-market gate + batched Gemini geocoding
        ├── _employment.py        # employment-type classifier (lead tag)
        ├── _http.py              # curl_cffi client (browser-TLS impersonation)
        ├── _browser.py           # Playwright headless-Chromium renderer
        ├── _greenhouse.py _workday.py _jobvite.py _ultipro.py   # reusable ATS adapters
        ├── firms.py              # firm registry → dispatches to ATS adapters
        ├── archinect.py dezeen.py hdr.py          # architecture boards
        ├── duke.py harvard.py wordpress_psych.py fun.py   # psychology boards
        ├── indeed.py             # SerpAPI Google Jobs (psychology)
        └── appic.py              # parked (doctoral board; wrong audience)
```

## Source roster (13 live)

**Architecture** — `archinect` (HTML), `dezeen` (HTML via curl_cffi), `hdr`
(Taleo, via Playwright), and `firms.py`:

| Firm | ATS | Adapter |
| --- | --- | --- |
| DLR Group, CannonDesign | Greenhouse | `_greenhouse.py` |
| Gensler, HKS, KPF, SOM | Workday | `_workday.py` |
| NBBJ | Jobvite | `_jobvite.py` |
| Perkins&Will | UKG/UltiPro | `_ultipro.py` |

**Psychology** — `wordpress_psych` (WP REST API), `duke` (HTML), `harvard`
(post-grad research jobs, via curl_cffi), `fun` (summer programs, **semester**
cadence), `indeed` (SerpAPI Google Jobs).

## Tools & libraries

| Tool | Used for |
| --- | --- |
| `requests` + `beautifulsoup4` | HTTP + HTML parsing (default path) |
| `curl_cffi` | Browser-TLS impersonation for WAF-guarded boards (Dezeen, Harvard) — see `_http.py` |
| `playwright` (Chromium) | JS-rendered boards that no HTTP client can reach (HDR) — see `_browser.py` |
| `google-search-results` (SerpAPI) | Google Jobs search (`indeed.py`) |
| Google **Gemini** (`gemini-flash-lite-latest`, REST) | Extracts a city from a job description when the structured location is vague — batched in `_location.py` |
| `python-dotenv` | Loads `../.env` for local runs (no-op in CI) |

## Secrets

Set both as **GitHub Actions secrets** (repo → Settings → Secrets and variables →
Actions) for CI, and in a local **`.env`** at the repo root for dev runs
(gitignored). Both degrade gracefully when absent.

| Secret | Purpose | If unset |
| --- | --- | --- |
| `SERPAPI_KEY` | SerpAPI Google Jobs (`indeed.py`); free tier 250/mo, ~4 searches/run | `indeed` returns `[]` |
| `GEMINI_API_KEY` | Gemini location fallback (`_location.py`) | LLM step skipped; rules-only geocoding |

## Workflow & triggers

`.github/workflows/scrape.yml`:

- **Schedule:** daily at 08:00 UTC. **Manual:** Actions tab → *Scrape jobs* →
  *Run workflow* (`workflow_dispatch`).
- Steps: checkout → install deps → `playwright install --with-deps chromium` →
  `python pipeline.py` → commit `README.md` + `data/*.json` back to `master`.
- **`RUN_ALL_SOURCES=1`** (env) ignores cadence and runs every source — use it
  for manual runs / local testing to exercise low-frequency sources.

## Filters, tags & cadence

- **Undergrad filter** (`_filter.py`): DROP advanced-degree/licensure terms
  (always) and an opt-in seniority screen (Senior/Manager/level II+/N+ years) for
  broad title-based sources; KEEP requires an entry-level signal on broad sources,
  and is relaxed for curated ones.
- **Location gate** (`_location.py`): deterministic rules first (state/city
  matching, non-target fast-drop), then one **batched** Gemini call resolves the
  vague remainder. Targets: California (whole state) + Seattle / NYC / Austin /
  Dallas / Houston, plus remote.
- **Employment tags** (`_employment.py`): every listing leads with Internship /
  Full-Time / Part-Time / Contract / Volunteer, then domain tags.
- **Cadence** (`pipeline.Source`): `daily` | `weekly` | `monthly` | `semester`.
  `merge` is source-aware — a listing is deactivated only if its source ran this
  cycle, so skipped low-frequency sources (and transient failures) keep their
  listings live.

## Listing schema (`data/*.json`)

`id` (sha256 of title+company), `title`, `company`, `location`, `url`,
`posted_date` (ISO or ""), `scraped_date`, `active` (bool), `tags` (list),
`source` (scraper name). `_description` is a transient field some scrapers attach
for the location gate; it is stripped before save.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate
pip install -r jobs/requirements.txt
python -m playwright install chromium                # for the HDR scraper
# create jobs-board/.env with SERPAPI_KEY=... and GEMINI_API_KEY=...

cd jobs
python pipeline.py                    # full run (honors cadence)
RUN_ALL_SOURCES=1 python pipeline.py  # force every source
python -m scrapers.dezeen             # run one scraper standalone
```

Scrapers use package-relative imports, so run them as `python -m scrapers.<name>`
from `jobs/` — **not** `python jobs/scrapers/<name>.py`.

**Adding a firm:** add one entry to `FIRMS` in `firms.py` — Greenhouse needs a
board `slug`; Workday needs `host`/`tenant`/`site`; Jobvite a `slug`; UltiPro a
`host`/`code`/`board_id` (all readable from the firm's careers URL / network tab).

## Dead-ends (verified — do not re-attempt without new info)

| Source | Why it's out |
| --- | --- |
| APA PsycCareers | Board closed 2026-07-31, no replacement |
| APPIC | Doctoral-internship board — wrong audience (parked in `appic.py`) |
| Society boards (SPSP, APS, SRCD, SAS) | Faculty/postdoc recruitment → ~0 undergrad yield after filtering; SRCD also a JS shell |
| Corgan (iCIMS) | Interactive "Human Verification" CAPTCHA — blocks headless Chromium too |
| AIA career center | Cloudflare "One moment…" challenge — blocks headless Chromium too |
| NSF REU | Redesigned to a JS/challenge app (HTTP 202) with no reachable API |
| NIH SIP, Yale Child Study | Single non-target locations (Bethesda MD / New Haven CT) — the location gate drops them |
| ArchDaily / Coroflot | No job board / product-design only (~1 architecture role) |

Reaching Corgan/AIA/NSF-REU would require a CAPTCHA-solving service (paid, against
ToS) — intentionally not pursued.
