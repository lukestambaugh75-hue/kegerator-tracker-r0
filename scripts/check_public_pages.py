#!/usr/bin/env python3
"""Check the public GitHub Pages dashboard."""

from __future__ import annotations

import json
import sys
import urllib.request
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


PUBLIC_URL = "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/"
PUBLIC_LISTINGS_URL = urljoin(PUBLIC_URL, "data/listings.json")


def fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "LukeKegeratorTracker/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read()


def validate_public_body(body: bytes, allowed_listing_urls: set[str] | frozenset[str]) -> None:
    if not isinstance(body, bytes):
        raise AudienceBoundaryError("public dashboard must be validated as exact response bytes")
    validate_html(
        body,
        allowed_listing_urls=allowed_listing_urls,
        asset_root=ROOT,
        source_path=PUBLIC_URL,
    )
    text = body.decode("utf-8")
    required = ["Kegerator Tracker", "data/listings.json", "data/specs.json", "history.csv"]
    missing = [value for value in required if value not in text]
    if missing:
        raise AudienceBoundaryError(f"public dashboard missing: {missing}")


def main() -> None:
    status, raw_body = fetch(PUBLIC_URL)
    if status != 200:
        raise SystemExit(f"unexpected dashboard status {status}")
    listings_status, raw_listings = fetch(PUBLIC_LISTINGS_URL)
    if listings_status != 200:
        raise SystemExit(f"unexpected listings status {listings_status}")
    listings = json.loads(raw_listings.decode("utf-8"))
    validate_public_body(
        raw_body,
        listing_source_urls(listings),
    )
    print(f"public dashboard ok: {PUBLIC_URL}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"public dashboard check failed: {exc}", file=sys.stderr)
        raise
