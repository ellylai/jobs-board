"""Shared HTTP client for WAF-guarded sites.

Some boards (Dezeen, and other Phase-2 sources) sit behind a WAF that fingerprints
the TLS/HTTP2 handshake and 403s stock Python clients (requests/urllib3) even with
a full browser header set. ``curl_cffi`` performs the request through curl while
impersonating a real browser's TLS stack, which the WAF accepts.

The WAF blocks probabilistically -- the same fingerprint returns 200 on one
request and 403 on the next, and it flags the very newest Chrome builds -- so we
rotate through several impersonation targets and return the first response that
isn't a client/server error. The returned object is requests-API-compatible
(``.content``, ``.text``, ``.status_code``, ``.raise_for_status()``).

For ordinary, unguarded sites keep using plain ``requests`` -- this is only for
sites that reject it.
"""

from __future__ import annotations

import logging
import time

from curl_cffi import requests as _cr

log = logging.getLogger("scrapers.http")

# Ordered by how reliably the WAFs seen so far accept them.
IMPERSONATE_TARGETS = ("safari17_0", "chrome120", "chrome116", "safari15_5", "chrome110")
DEFAULT_TIMEOUT = 30
_RETRY_DELAY = 0.5              # between fingerprints within a round
MAX_ROUNDS = 3                 # full passes over the fingerprints
_BACKOFF_BASE = 1.5            # seconds before the 2nd round, doubling thereafter
# Block-like statuses worth retrying (WAF/rate-limit/transient). A plain 404/401
# is a real answer, so we return it immediately instead of hammering.
RETRYABLE_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 520, 521, 522, 523, 524, 525})


def get(url: str, *, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT, **kwargs):
    """GET ``url`` via curl_cffi, rotating browser TLS fingerprints on rejection.

    The WAFs block probabilistically, so a full pass over the fingerprints can
    still fail; we retry the whole pass up to ``MAX_ROUNDS`` times with an
    exponential backoff between rounds. Returns the first response with status
    < 400 (or the first non-retryable status, e.g. a genuine 404). If every
    attempt is a block-like status, returns the last response so the caller's
    ``raise_for_status`` surfaces it; if every attempt raised, re-raises the last.
    """
    last_response = None
    last_error: Exception | None = None
    for round_no in range(MAX_ROUNDS):
        if round_no:
            backoff = _BACKOFF_BASE * (2 ** (round_no - 1))
            log.debug("%s: all fingerprints rejected; backoff %.1fs (round %d/%d)",
                      url, backoff, round_no + 1, MAX_ROUNDS)
            time.sleep(backoff)
        for imp in IMPERSONATE_TARGETS:
            try:
                resp = _cr.get(url, impersonate=imp, headers=headers, timeout=timeout, **kwargs)
            except Exception as exc:  # noqa: BLE001 - try the next fingerprint
                last_error = exc
                continue
            if resp.status_code < 400 or resp.status_code not in RETRYABLE_STATUSES:
                return resp  # success, or a real (non-retryable) error like 404
            last_response = resp
            log.debug("%s rejected %s (HTTP %s); rotating fingerprint", url, imp, resp.status_code)
            time.sleep(_RETRY_DELAY)
    if last_response is not None:
        return last_response
    raise last_error if last_error else RuntimeError(f"No response for {url}")
