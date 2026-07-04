#!/usr/bin/env python3
"""Check the public GitHub Pages dashboard."""

import sys
import urllib.request


PUBLIC_URL = "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/"


def main() -> None:
    request = urllib.request.Request(PUBLIC_URL, headers={"User-Agent": "LukeKegeratorTracker/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="ignore")
    if response.status != 200:
        raise SystemExit(f"unexpected status {response.status}")
    required = ["Kegerator Tracker", "data/listings.json", "Deal Trackers", "Main Dashboard"]
    missing = [text for text in required if text not in body]
    if missing:
        raise SystemExit(f"public dashboard missing: {missing}")
    print(f"public dashboard ok: {PUBLIC_URL}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"public dashboard check failed: {exc}", file=sys.stderr)
        raise
