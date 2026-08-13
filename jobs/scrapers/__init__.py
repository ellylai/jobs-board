"""Scraper package.

Each scraper module is independent and importable, and exposes a ``scrape()``
function (or track-specific functions) returning a list of listing dicts with
the shared schema:

    {
        "id": str,            # sha256(title + company)
        "title": str,
        "company": str,
        "location": str,
        "url": str,
        "posted_date": str,   # ISO 8601 (YYYY-MM-DD)
        "scraped_date": str,  # ISO 8601 (YYYY-MM-DD)
        "active": bool,
        "tags": list[str],
    }
"""
