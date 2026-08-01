#!/usr/bin/env python3
"""Build and optionally publish the default-off encrypted Kegerator pilot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOOLS_ROOT = ROOT.parent / "Tools" / "encrypted-dashboard-publisher"
DEFAULT_PUBLISHER_ROOT = ROOT.parent / "Encrypted Tracker Link Publisher r0"
DEFAULT_RECEIPT = (
    Path.home()
    / ".config"
    / "encrypted-dashboard-publisher"
    / "receipts"
    / "kegerator-pilot.json"
)
BASE_URL = "https://lukestambaugh75-hue.github.io/encrypted-tracker-link-publisher-r0"
PILOT_FLAG = "KGERATOR_ENCRYPTED_DASHBOARD_PILOT"
BINDING_ID = "binding_9YcJ2xQw8Vn4Lm7Rt5Kp3Hs6"
EXPECTED_RECIPIENTS = ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audience_guard import (  # noqa: E402
    listing_source_urls,
    validate_encrypted_pilot_receipt,
)
from scripts.refresh_state import (  # noqa: E402
    build_payload_source_identity,
    evaluate_refresh,
    utc_iso,
)
from tools.build_email import _snapshot_is_represented, best_rows, money  # noqa: E402


class EncryptedPilotError(RuntimeError):
    """The Kegerator encrypted pilot cannot safely continue."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_history(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _price(value: object) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _source_history(rows: list[dict], source_url: str) -> list[dict]:
    matched = [row for row in rows if row.get("source") == source_url]
    matched.sort(key=lambda row: row.get("date") or "")
    return matched[-30:]


def _prior_price(rows: list[dict], source_url: str, current_date: str) -> float | None:
    earlier = [
        _price(row.get("price"))
        for row in _source_history(rows, source_url)
        if (row.get("date") or "") < current_date and _price(row.get("price")) is not None
    ]
    return earlier[-1] if earlier else None


def _spec_index(specs: list[dict]) -> dict[tuple[str, str], dict]:
    return {
        (str(row.get("brand") or "").casefold(), str(row.get("model") or "").casefold()): row
        for row in specs
    }


def _details(listing: dict, spec: dict) -> dict[str, str]:
    def yes_no(value: object) -> str:
        return "Yes" if value else "No"

    low = spec.get("temp_low_f")
    high = spec.get("temp_high_f")
    temperature = "Not documented"
    if low is not None and high is not None:
        temperature = f"{low}-{high} F"
    dimensions = spec.get("dims_hwd_in") or "Not documented"
    return {
        "Retailer": str(listing.get("retailer") or "Not stated"),
        "Description": str(listing.get("description") or "Not stated"),
        "Tap count": str(listing.get("tap_count") or spec.get("tap_count") or "Not stated"),
        "Finish": str(listing.get("finish") or spec.get("finish") or "Not stated"),
        "Complete kit": yes_no(listing.get("complete_kit")),
        "Outdoor rated": yes_no(listing.get("outdoor_rated")),
        "Garage suitability": str(listing.get("garage_suitability") or "Not stated"),
        "Temperature range": temperature,
        "Keg capacity": str(spec.get("keg_capacity") or "Not documented"),
        "Dimensions H x W x D": str(dimensions),
        "Digital control": yes_no(spec.get("digital_control")),
        "Fan forced": yes_no(spec.get("fan_forced")),
        "Notes": str(spec.get("notes") or "No additional notes"),
    }


def _row_status(listing: dict, top_urls: set[str]) -> str:
    if listing.get("source_url") in top_urls:
        return "Top pick"
    if listing.get("data_quality") == "blocked":
        return "Blocked"
    return "Available" if listing.get("in_stock") else "Unavailable"


def build_dashboard(
    listings: list[dict],
    specs: list[dict],
    refresh_status: dict,
    history_rows: list[dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Build the full private dashboard without writing plaintext to disk."""
    now = now or datetime.now(timezone.utc)
    state = evaluate_refresh(refresh_status, now=now)
    if state["state"] not in {"Fresh", "Due"} or not _snapshot_is_represented(
        listings, refresh_status
    ):
        raise EncryptedPilotError(
            f"current source evidence is not publishable: {state['state']}"
        )
    allowed_sources = listing_source_urls(listings)
    source_identity = build_payload_source_identity(listings, specs, refresh_status)
    snapshot_id = "snapshot_" + hashlib.sha256(_canonical_bytes(source_identity)).hexdigest()
    selected = best_rows(listings)
    top_urls = {
        row["source_url"] for row in selected.values() if isinstance(row, dict)
    }
    spec_by_model = _spec_index(specs)
    current_date = str(refresh_status["data_refreshed_at_utc"])[:10]
    dashboard_rows = []
    changed = 0
    for index, listing in enumerate(listings):
        source_url = str(listing["source_url"])
        if source_url not in allowed_sources:
            raise EncryptedPilotError("listing source URL escaped the audience allowlist")
        current_price = _price(listing.get("current_price"))
        prior = _prior_price(history_rows, source_url, current_date)
        change = None if current_price is None or prior is None else round(current_price - prior, 2)
        if change not in (None, 0):
            changed += 1
        model = str(listing.get("model") or listing.get("source_model") or "")
        spec = spec_by_model.get(
            (str(listing.get("brand") or "").casefold(), model.casefold()), {}
        )
        history = [
            {
                "at": str(entry.get("date") or ""),
                "value": money(_price(entry.get("price"))),
                "note": str(entry.get("data_quality") or ""),
            }
            for entry in _source_history(history_rows, source_url)
        ]
        dashboard_rows.append(
            {
                "id": f"offer-{index + 1:02d}",
                "name": f"{listing.get('brand', '')} {model}".strip(),
                "model": model,
                "price": current_price,
                "change": change,
                "availability": "In stock" if listing.get("in_stock") else "Not in stock",
                "freshness": f"Confirmed {listing.get('retrieved')}",
                "status": _row_status(listing, top_urls),
                "confidence": "Confirmed" if listing.get("data_quality") == "confirmed" else str(listing.get("data_quality") or "Unknown").title(),
                "validation": f"Direct retailer evidence checked {listing.get('retrieved')}",
                "direct_url": source_url,
                "history": history,
                "details": _details(listing, spec),
            }
        )

    recommendation = "; ".join(
        [
            f"lowest complete single tap: {row['brand']} {row['model']} at {money(row['current_price'])} from {row['retailer']}"
            for row in [selected.get("single")]
            if row
        ]
        + [
            f"lowest complete dual tap: {row['brand']} {row['model']} at {money(row['current_price'])} from {row['retailer']}"
            for row in [selected.get("dual")]
            if row
        ]
        + [
            f"lowest outdoor-rated: {row['brand']} {row['model']} at {money(row['current_price'])} from {row['retailer']}"
            for row in [selected.get("outdoor")]
            if row
        ]
    )
    if not recommendation:
        recommendation = "No actionable current recommendation; review the evidence rows."
    return {
        "schema_version": 1,
        "product_id": "kegerator-tracker",
        "title": "Kegerator Deal Tracker",
        "snapshot_id": snapshot_id,
        "generated_at": utc_iso(now),
        "source_freshness": f"{state['state']} - refreshed {refresh_status['data_refreshed_at_utc']}",
        "overall_status": state["state"].upper(),
        "summary": {
            "decision": f"Compare {len(dashboard_rows)} current Kegerator offers",
            "recommendation": recommendation,
            "verified_changes": changed,
            "blocked": sum(row.get("data_quality") == "blocked" for row in listings),
            "stale": 0,
            "retry": 0,
            "overdue": 0,
        },
        "rows": dashboard_rows,
    }


def load_current_dashboard(*, now: datetime | None = None) -> dict:
    return build_dashboard(
        _load_json(ROOT / "data" / "listings.json"),
        _load_json(ROOT / "data" / "specs.json"),
        _load_json(ROOT / "data" / "refresh-status.json"),
        _load_history(ROOT / "history.csv"),
        now=now,
    )


def _load_publisher(tools_root: Path):
    tools_root = tools_root.resolve()
    if not (tools_root / "encrypted_dashboard_publisher").is_dir():
        raise EncryptedPilotError(f"shared publisher package is missing: {tools_root}")
    sys.path.insert(0, os.fspath(tools_root))
    schema = importlib.import_module("encrypted_dashboard_publisher.schema")
    transaction = importlib.import_module("encrypted_dashboard_publisher.git_transaction")
    return schema.validate_dashboard, transaction.publish_git_fresh


def publish_current(
    *,
    publisher_root: Path = DEFAULT_PUBLISHER_ROOT,
    tools_root: Path = DEFAULT_TOOLS_ROOT,
    receipt_path: Path = DEFAULT_RECEIPT,
    now: datetime | None = None,
) -> dict:
    if os.environ.get(PILOT_FLAG) != "1":
        raise EncryptedPilotError(f"publication is disabled; set {PILOT_FLAG}=1 explicitly")
    dashboard = load_current_dashboard(now=now)
    validate_dashboard, publish_git_fresh = _load_publisher(tools_root)
    dashboard = validate_dashboard(dashboard)
    result = publish_git_fresh(
        dashboard,
        publisher_root=publisher_root,
        viewer_source=tools_root / "viewer",
        binding_id=BINDING_ID,
        base_url=BASE_URL,
        link_output=receipt_path,
    )
    receipt = _load_json(receipt_path)
    validate_encrypted_pilot_receipt(
        receipt,
        expected_snapshot_id=dashboard["snapshot_id"],
        expected_binding_id=BINDING_ID,
    )
    return {
        "commit": result["commit"],
        "snapshot_id": result["snapshot_id"],
        "ciphertext_sha256": result["ciphertext_sha256"],
        "audience_id": result["audience_id"],
        "row_count": len(dashboard["rows"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--publisher-root", type=Path, default=DEFAULT_PUBLISHER_ROOT)
    parser.add_argument("--tools-root", type=Path, default=DEFAULT_TOOLS_ROOT)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    args = parser.parse_args()
    try:
        if args.publish:
            result = publish_current(
                publisher_root=args.publisher_root,
                tools_root=args.tools_root,
                receipt_path=args.receipt,
            )
            print(
                "encrypted Kegerator pilot published: "
                f"rows={result['row_count']} commit={result['commit']}"
            )
        else:
            dashboard = load_current_dashboard()
            validate_dashboard, _ = _load_publisher(args.tools_root)
            validated = validate_dashboard(dashboard)
            print(
                "encrypted Kegerator pilot validation passed: "
                f"rows={len(validated['rows'])}; publication remains disabled"
            )
    except Exception as exc:
        print(f"encrypted Kegerator pilot stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
