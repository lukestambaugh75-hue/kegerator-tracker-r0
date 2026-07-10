#!/usr/bin/env python3
"""Build a reviewable email payload for the kegerator tracker."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
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
from scripts.refresh_state import evaluate_refresh, format_central, utc_iso  # noqa: E402


LISTINGS_PATH = ROOT / "data" / "listings.json"
SPECS_PATH = ROOT / "data" / "specs.json"
STATUS_PATH = ROOT / "data" / "refresh-status.json"
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


def _snapshot_is_represented(listings: list[dict], refresh: dict) -> bool:
    success_at = utc_iso(refresh.get("data_refreshed_at_utc"))
    if not success_at or not listings:
        return False
    quality = refresh.get("quality_counts") or {}
    if refresh.get("row_count") != len(listings) or refresh.get("source_count") != len(listings):
        return False
    if quality != {"verified": len(listings), "estimated": 0, "blocked": 0}:
        return False
    return all(
        row.get("data_quality") == "confirmed" and utc_iso(row.get("retrieved")) == success_at
        for row in listings
    )


def _sanitized_reason(value: object) -> str:
    return re.sub(r"\$\s?\d[\d,]*(?:\.\d{1,2})?", "[amount withheld]", str(value or "Not recorded"))


def build_payload(
    listings: list[dict],
    specs: list[dict],
    refresh_status: dict,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    *,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    state = evaluate_refresh(refresh_status, now=now)
    status = state["state"]
    actionable = status in {"Fresh", "Due"} and _snapshot_is_represented(listings, refresh_status)
    success_text = state["data_refreshed_at_central"]
    attempt_text = state["last_attempt_at_central"]
    if state["last_attempt_status"] != "unknown":
        attempt_text = f"{attempt_text} ({state['last_attempt_status']})"
    reason = _sanitized_reason(state["reason"])
    body_lines = [
        "Kegerator tracker",
        "",
        f"Refresh state: {status}",
        f"Last successful data refresh: {success_text}",
        f"Latest attempt: {attempt_text}",
        f"State reason: {reason}",
        f"Dashboard: {dashboard_url}",
    ]
    html_lines = [
        "<h2>Kegerator tracker</h2>",
        f"<p><strong>Refresh state:</strong> {html.escape(status)}</p>",
        f"<p><strong>Last successful data refresh:</strong> {html.escape(success_text)}</p>",
        f"<p><strong>Latest attempt:</strong> {html.escape(attempt_text)}</p>",
        f"<p><strong>State reason:</strong> {html.escape(reason)}</p>",
        f"<p>Dashboard: <a href='{html.escape(dashboard_url)}'>{html.escape(dashboard_url)}</a></p>",
    ]
    if actionable:
        best = best_rows(listings)
        outdoor_count = sum(1 for row in specs if row.get("outdoor_rated"))
        body_lines.extend(
            [
                "",
                row_line("Lowest complete single tap", best["single"]),
                row_line("Lowest complete dual tap", best["dual"]),
                row_line("Lowest outdoor-rated", best["outdoor"]),
                f"Models tracked: {len(specs)}",
                f"Outdoor-rated models tracked: {outdoor_count}",
                "",
                "Confirm final cart total, delivery timing, seller identity, and stock before buying.",
            ]
        )
        html_lines.extend(
            [
                f"<p>{html.escape(row_line('Lowest complete single tap', best['single']))}</p>",
                f"<p>{html.escape(row_line('Lowest complete dual tap', best['dual']))}</p>",
                f"<p>{html.escape(row_line('Lowest outdoor-rated', best['outdoor']))}</p>",
                f"<p>Models tracked: {len(specs)}. Outdoor-rated models tracked: {outdoor_count}.</p>",
                "<p style='color:#666;font-size:12px'>Confirm final cart total, delivery timing, seller identity, and stock before buying.</p>",
            ]
        )
    payload = {
        "to": list(RECIPIENTS),
        "cc": [],
        "bcc": [],
        "subject": f"Kegerator tracker - {status}",
        "body_text": "\n".join(body_lines),
        "body_html": "\n".join(html_lines),
        "dashboard_url": dashboard_url,
        "generated_at": utc_iso(now),
        "refresh_state": status,
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
    refresh_status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    payload = build_payload(listings, specs, refresh_status, args.dashboard_url)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "latest-email.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"email payload: recipients={len(payload['to'])} subject={payload['subject']!r}")


if __name__ == "__main__":
    main()
