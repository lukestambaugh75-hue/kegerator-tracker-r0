#!/usr/bin/env python3
"""Build a reviewable email payload for the kegerator tracker."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audience_guard import (  # noqa: E402
    CANONICAL_DASHBOARD_URL,
    EXPECTED_RECIPIENTS,
    listing_source_urls,
    validate_email_payload,
)


LISTINGS_PATH = ROOT / "data" / "listings.json"
SPECS_PATH = ROOT / "data" / "specs.json"
DEFAULT_DASHBOARD_URL = CANONICAL_DASHBOARD_URL
RECIPIENTS = list(EXPECTED_RECIPIENTS)


def money(value) -> str:
    if value in (None, ""):
        return "not shown"
    return f"${float(value):,.2f}".replace(".00", "")


def best_rows(listings: list[dict]) -> dict[str, dict | None]:
    available = [row for row in listings if row.get("in_stock") and row.get("current_price") is not None and row.get("complete_kit")]
    singles = [row for row in available if row.get("tap_count") == 1]
    duals = [row for row in available if row.get("tap_count") == 2]
    outdoor = [row for row in available if row.get("outdoor_rated")]
    return {
        "single": min(singles, key=lambda row: row["current_price"]) if singles else None,
        "dual": min(duals, key=lambda row: row["current_price"]) if duals else None,
        "outdoor": min(outdoor, key=lambda row: row["current_price"]) if outdoor else None,
    }


def row_line(label: str, row: dict | None) -> str:
    if not row:
        return f"{label}: no current row"
    return (
        f"{label}: {row['retailer']} - {row['brand']} {row['model']} - "
        f"{money(row['current_price'])} - {row['garage_suitability']}"
    )


def build_payload(listings: list[dict], specs: list[dict], dashboard_url: str = DEFAULT_DASHBOARD_URL) -> dict:
    best = best_rows(listings)
    outdoor_count = sum(1 for row in specs if row.get("outdoor_rated"))
    body_lines = [
        "Kegerator price tracker",
        "",
        row_line("Lowest complete single tap", best["single"]),
        row_line("Lowest complete dual tap", best["dual"]),
        row_line("Lowest outdoor-rated", best["outdoor"]),
        f"Models tracked: {len(specs)}",
        f"Outdoor-rated models tracked: {outdoor_count}",
        "",
        f"Dashboard: {dashboard_url}",
        "",
        "Garage note: outdoor rating and low-temperature headroom matter in a hot Houston garage. Confirm final cart total, delivery timing, seller identity, and current stock before buying.",
    ]
    html_lines = [
        "<h2>Kegerator price tracker</h2>",
        f"<p>{row_line('Lowest complete single tap', best['single'])}</p>",
        f"<p>{row_line('Lowest complete dual tap', best['dual'])}</p>",
        f"<p>{row_line('Lowest outdoor-rated', best['outdoor'])}</p>",
        f"<p>Models tracked: {len(specs)}. Outdoor-rated models tracked: {outdoor_count}.</p>",
        f"<p>Dashboard: <a href='{dashboard_url}'>{dashboard_url}</a></p>",
        "<p style='color:#666;font-size:12px'>Confirm final cart total, delivery timing, seller identity, and current stock before buying.</p>",
    ]
    payload = {
        "to": list(RECIPIENTS),
        "cc": [],
        "bcc": [],
        "subject": "Kegerator tracker - garage-ready price watch",
        "body_text": "\n".join(body_lines),
        "body_html": "\n".join(html_lines),
        "dashboard_url": dashboard_url,
    }
    validate_email_payload(payload, listing_source_urls(listings))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "out"))
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    args = parser.parse_args()
    listings = json.loads(LISTINGS_PATH.read_text(encoding="utf-8"))
    specs = json.loads(SPECS_PATH.read_text(encoding="utf-8"))
    payload = build_payload(listings, specs, args.dashboard_url)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "latest-email.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"email payload: recipients={len(payload['to'])} subject={payload['subject']!r}")


if __name__ == "__main__":
    main()
