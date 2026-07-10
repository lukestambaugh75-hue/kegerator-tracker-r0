#!/usr/bin/env python3
"""Check the public GitHub Pages dashboard."""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audience_guard import (  # noqa: E402
    AudienceBoundaryError,
    listing_source_urls,
    validate_html,
)
from scripts.refresh_state import evaluate_refresh, utc_iso, validate_refresh_status  # noqa: E402


PUBLIC_URL = "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/"
PUBLIC_LISTINGS_URL = urljoin(PUBLIC_URL, "data/listings.json")
PUBLIC_STATUS_URL = urljoin(PUBLIC_URL, "data/refresh-status.json")


def fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "LukeKegeratorTracker/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read()


def validate_public_body(
    body: bytes,
    allowed_listing_urls: set[str] | frozenset[str],
    refresh_status: dict,
) -> None:
    if not isinstance(body, bytes):
        raise AudienceBoundaryError("public dashboard must be validated as exact response bytes")
    validate_html(
        body,
        allowed_listing_urls=allowed_listing_urls,
        asset_root=ROOT,
        source_path=PUBLIC_URL,
    )
    text = body.decode("utf-8")
    validate_refresh_status(refresh_status)
    required = [
        "Kegerator Tracker",
        "data/listings.json",
        "data/specs.json",
        "data/refresh-status.json",
        "history.csv",
        "data_refreshed_at_utc",
        "Last successful data refresh",
        "Historical only",
    ]
    missing = [value for value in required if value not in text]
    if missing:
        raise AudienceBoundaryError(f"public dashboard missing: {missing}")


def validate_public_status(
    refresh_status: dict,
    listings: list[dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Prove public metadata represents the successful snapshot and visible state."""
    refresh = validate_refresh_status(refresh_status)
    if not isinstance(listings, list) or not listings:
        raise AudienceBoundaryError("public listings must be a non-empty array")
    if refresh["source_count"] != len(listings) or refresh["row_count"] != len(listings):
        raise AudienceBoundaryError("public refresh counts do not match public listings")
    expected_quality = {"verified": len(listings), "estimated": 0, "blocked": 0}
    if refresh["quality_counts"] != expected_quality:
        raise AudienceBoundaryError("public refresh quality counts do not represent the successful snapshot")
    success_at = refresh["data_refreshed_at_utc"]
    if not success_at:
        raise AudienceBoundaryError("public status must record a successful data refresh")
    for index, row in enumerate(listings):
        if row.get("data_quality") != "confirmed":
            raise AudienceBoundaryError(f"public listing {index} is not confirmed historical evidence")
        if utc_iso(row.get("retrieved")) != success_at:
            raise AudienceBoundaryError(f"public listing {index} is not from the successful snapshot")
    return evaluate_refresh(refresh, now=now or datetime.now(timezone.utc))


def main() -> None:
    status, raw_body = fetch(PUBLIC_URL)
    if status != 200:
        raise SystemExit(f"unexpected dashboard status {status}")
    listings_status, raw_listings = fetch(PUBLIC_LISTINGS_URL)
    if listings_status != 200:
        raise SystemExit(f"unexpected listings status {listings_status}")
    listings = json.loads(raw_listings.decode("utf-8"))
    refresh_status_code, raw_refresh_status = fetch(PUBLIC_STATUS_URL)
    if refresh_status_code != 200:
        raise SystemExit(f"unexpected refresh-status status {refresh_status_code}")
    refresh_status = json.loads(raw_refresh_status.decode("utf-8"))
    state = validate_public_status(refresh_status, listings)
    validate_public_body(
        raw_body,
        listing_source_urls(listings),
        refresh_status,
    )
    print(
        f"public dashboard ok: {PUBLIC_URL} state={state['state']} "
        f"data_refreshed_at={state['data_refreshed_at_utc']}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"public dashboard check failed: {exc}", file=sys.stderr)
        raise
