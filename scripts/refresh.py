#!/usr/bin/env python3
"""Refresh kegerator listings and append price history."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LISTINGS_PATH = ROOT / "data" / "listings.json"
SPECS_PATH = ROOT / "data" / "specs.json"
HISTORY_PATH = ROOT / "history.csv"
CACHE_DIR = ROOT / ".cache" / "http"
HISTORY_FIELDS = ["date", "brand", "model", "retailer", "price", "list_price", "source", "data_quality"]
USER_AGENT = "LukeKegeratorTracker/1.0 (+https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/)"
MIN_REQUEST_SECONDS = 3.1
PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def today_utc(dt: datetime | None = None) -> str:
    return (dt or utc_now()).date().isoformat()


def load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def spec_key(brand: str, model: str) -> str:
    return f"{brand}::{model}"


def compute_garage_suitability(spec: dict) -> str:
    if spec.get("outdoor_rated"):
        return "Best - outdoor rated"
    if spec.get("deep_chill") and spec.get("fan_forced"):
        return "Good - deep-chill + fan-forced"
    temp_low = spec.get("temp_low_f")
    if isinstance(temp_low, (int, float)) and temp_low <= 32:
        return "Good - low-30s headroom"
    if isinstance(temp_low, (int, float)) and temp_low >= 35:
        return "Fair - limited cold headroom"
    return "Fair - limited cold headroom"


def discount_pct(current_price, list_price):
    if current_price in (None, "") or list_price in (None, "", 0):
        return None
    try:
        current = float(current_price)
        list_amount = float(list_price)
    except (TypeError, ValueError):
        return None
    if list_amount <= 0:
        return None
    return round((list_amount - current) / list_amount * 100, 2)


def normalize_specs(specs: list[dict]) -> list[dict]:
    normalized = []
    for raw in specs:
        spec = dict(raw)
        spec["garage_suitability"] = compute_garage_suitability(spec)
        normalized.append(spec)
    return sorted(normalized, key=lambda row: (row["brand"].lower(), row["model"].lower()))


def normalize_listing(listing: dict, specs_by_key: dict[str, dict], retrieved: str | None = None) -> dict:
    row = dict(listing)
    key = spec_key(row["brand"], row["model"])
    spec = specs_by_key.get(key, {})
    for field in ["tap_count", "finish", "type", "complete_kit", "outdoor_rated"]:
        if field in spec:
            row[field] = spec[field]
    row["garage_suitability"] = compute_garage_suitability(spec or row)
    row["discount_pct"] = discount_pct(row.get("current_price"), row.get("list_price"))
    row["in_stock"] = bool(row.get("in_stock", True))
    row["retrieved"] = retrieved or row.get("retrieved") or iso_z(utc_now())
    row["data_quality"] = row.get("data_quality") or "estimated"
    return row


def robots_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    try:
        parser.set_url(robots_url)
        parser.read()
    except Exception:
        return False
    return parser.can_fetch(USER_AGENT, url)


def cache_path_for(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.html"


def fetch_url(url: str, use_cache: bool = True) -> str | None:
    if not url.startswith("https://"):
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = cache_path_for(url)
    if use_cache and cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="ignore")
    if not robots_allowed(url):
        return None
    time.sleep(MIN_REQUEST_SECONDS)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError):
        return None
    cache_path.write_text(body, encoding="utf-8")
    return body


def parse_price(html: str | None) -> float | None:
    if not html:
        return None
    candidates = []
    for match in PRICE_RE.finditer(html[:500000]):
        value = match.group(1).replace(",", "")
        try:
            amount = float(value)
        except ValueError:
            continue
        if 100 <= amount <= 5000:
            candidates.append(amount)
    return min(candidates) if candidates else None


def try_live_price(row: dict, offline: bool) -> tuple[float | None, str]:
    if offline:
        return None, "offline"
    url = row.get("source_url") or ""
    parsed = urllib.parse.urlparse(url)
    if parsed.query or parsed.path.rstrip("/").endswith("/s"):
        return None, "search_url_skipped"
    html = fetch_url(url)
    amount = parse_price(html)
    return amount, "parsed" if amount is not None else "blocked_or_no_price"


def refresh_listings(listings: list[dict], specs: list[dict], now: datetime | None = None, offline: bool = False) -> list[dict]:
    now = now or utc_now()
    retrieved = iso_z(now)
    specs_by_key = {spec_key(row["brand"], row["model"]): row for row in specs}
    refreshed = []
    for raw in listings:
        row = normalize_listing(raw, specs_by_key, raw.get("retrieved"))
        live_price, status = try_live_price(row, offline=offline)
        if live_price is not None:
            row["current_price"] = live_price
            row["data_quality"] = "confirmed"
            row["retrieved"] = retrieved
        elif status != "offline" and row.get("current_price") is not None:
            same_day_seed = str(row.get("retrieved") or "").startswith(today_utc(now))
            if not (same_day_seed and row.get("data_quality") == "confirmed"):
                row["data_quality"] = "estimated" if row.get("data_quality") != "snapshot_varies" else "snapshot_varies"
        row = normalize_listing(row, specs_by_key, row.get("retrieved"))
        refreshed.append(row)
    refreshed.sort(key=lambda item: (item["brand"].lower(), item["model"].lower(), item["retailer"].lower()))
    return refreshed


def history_key(row: dict) -> tuple[str, str, str, str]:
    return (row["date"], row["brand"], row["model"], row["retailer"])


def format_amount(value) -> str:
    if value in (None, ""):
        return ""
    amount = float(value)
    text = f"{amount:.2f}"
    return text.rstrip("0").rstrip(".")


def append_history(listings: list[dict], path: Path = HISTORY_PATH, today: str | None = None) -> int:
    today = today or today_utc()
    existing = set()
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add(history_key(row))
    else:
        path.write_text(",".join(HISTORY_FIELDS) + "\n", encoding="utf-8")

    rows_to_append = []
    for item in listings:
        if item.get("current_price") in (None, ""):
            continue
        row = {
            "date": today,
            "brand": item["brand"],
            "model": item["model"],
            "retailer": item["retailer"],
            "price": format_amount(item.get("current_price")),
            "list_price": format_amount(item.get("list_price")),
            "source": item.get("source_url") or "",
            "data_quality": item.get("data_quality") or "estimated",
        }
        key = history_key(row)
        if key not in existing:
            rows_to_append.append(row)
            existing.add(key)

    if rows_to_append:
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, lineterminator="\n")
            writer.writerows(rows_to_append)
    return len(rows_to_append)


def main() -> None:
    offline = os.environ.get("KEG_TRACKER_OFFLINE") == "1"
    now = utc_now()
    specs = normalize_specs(load_json(SPECS_PATH))
    listings = load_json(LISTINGS_PATH)
    refreshed = refresh_listings(listings, specs, now=now, offline=offline)
    write_json(SPECS_PATH, specs)
    write_json(LISTINGS_PATH, refreshed)
    appended = append_history(refreshed, HISTORY_PATH, today_utc(now))
    print(f"refreshed {len(refreshed)} listings; appended {appended} history rows")
    if offline:
        print("offline mode: live fetch skipped")


if __name__ == "__main__":
    main()
