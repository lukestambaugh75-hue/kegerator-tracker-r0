#!/usr/bin/env python3
"""Refresh kegerator listings and append price history."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .refresh_state import apply_refresh_outcome, parse_utc, utc_iso
except ImportError:
    from refresh_state import apply_refresh_outcome, parse_utc, utc_iso


ROOT = Path(__file__).resolve().parents[1]
LISTINGS_PATH = ROOT / "data" / "listings.json"
SPECS_PATH = ROOT / "data" / "specs.json"
HISTORY_PATH = ROOT / "history.csv"
STATUS_PATH = ROOT / "data" / "refresh-status.json"
CACHE_DIR = ROOT / ".cache" / "http"
HISTORY_FIELDS = ["date", "brand", "model", "retailer", "price", "list_price", "source", "data_quality"]
USER_AGENT = "LukeKegeratorTracker/1.0 (+https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/)"
MIN_REQUEST_SECONDS = 3.1
PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)")
CENTRAL_ZONE = ZoneInfo("America/Chicago")


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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


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


def page_contains_model_identity(page_html: str | None, model: object) -> bool:
    """Require the expected model identity before accepting a page price."""
    return bool(_model_identity_spans(page_html, model))


def _model_identity_spans(page_html: str | None, model: object) -> list[tuple[int, int]]:
    if not page_html:
        return []
    parts = re.findall(r"[a-z0-9]+", str(model or "").casefold())
    if not parts:
        return []
    pattern = r"[^a-z0-9]*".join(re.escape(part) for part in parts)
    decoded = html_lib.unescape(page_html)
    return [match.span() for match in re.finditer(pattern, decoded, flags=re.IGNORECASE)]


def parse_model_bound_price(page_html: str | None, model: object) -> float | None:
    """Choose the valid price nearest an exact model occurrence, never a page-wide minimum."""
    if not page_html:
        return None
    decoded = html_lib.unescape(page_html)
    spans = _model_identity_spans(decoded, model)
    candidates: list[tuple[int, int, float]] = []
    for model_start, model_end in spans:
        window_start = max(0, model_start - 1200)
        window_end = min(len(decoded), model_end + 1200)
        for match in PRICE_RE.finditer(decoded, window_start, window_end):
            try:
                amount = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if not 100 <= amount <= 5000:
                continue
            if match.end() <= model_start:
                distance = model_start - match.end()
            elif match.start() >= model_end:
                distance = match.start() - model_end
            else:
                distance = 0
            candidates.append((distance, match.start(), amount))
    return min(candidates)[2] if candidates else None


def source_is_direct_product_page(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    segments = [segment.casefold() for segment in parsed.path.split("/") if segment]
    return not parsed.query and not any(segment in {"s", "search"} for segment in segments)


def try_live_price(row: dict, offline: bool) -> tuple[float | None, str]:
    if offline:
        return None, "offline"
    url = row.get("source_url") or ""
    if not source_is_direct_product_page(url):
        return None, "search_url_skipped"
    # A cache can help humans diagnose source changes, but it is never current
    # confirmation for a scheduled data attempt.
    html = fetch_url(url, use_cache=False)
    if html and not page_contains_model_identity(html, row.get("model")):
        return None, "identity_mismatch"
    amount = parse_model_bound_price(html, row.get("model"))
    return amount, "parsed" if amount is not None else "blocked_or_no_price"


def refresh_listings(
    listings: list[dict],
    specs: list[dict],
    now: datetime | None = None,
    offline: bool = False,
) -> tuple[list[dict], dict]:
    now = now or utc_now()
    retrieved = iso_z(now)
    specs_by_key = {spec_key(row["brand"], row["model"]): row for row in specs}
    refreshed = []
    confirmed_count = 0
    for raw in listings:
        row = normalize_listing(raw, specs_by_key, raw.get("retrieved"))
        live_price, status = try_live_price(row, offline=offline)
        if live_price is not None:
            row["current_price"] = live_price
            row["data_quality"] = "confirmed"
            row["retrieved"] = retrieved
            confirmed_count += 1
        else:
            row["data_quality"] = "blocked"
        row = normalize_listing(row, specs_by_key, row.get("retrieved"))
        refreshed.append(row)
    refreshed.sort(key=lambda item: (item["brand"].lower(), item["model"].lower(), item["retailer"].lower()))
    failed_count = len(refreshed) - confirmed_count
    if confirmed_count == len(refreshed) and refreshed:
        outcome_status = "success"
        reason = f"{confirmed_count} of {len(refreshed)} targets confirmed from current evidence."
    elif confirmed_count == 0:
        outcome_status = "blocked"
        reason = (
            f"0 of {len(refreshed)} targets were confirmed; source checks were blocked "
            "or did not return a current price."
        )
    else:
        outcome_status = "partial"
        reason = (
            f"{confirmed_count} of {len(refreshed)} targets were confirmed; "
            f"{failed_count} did not return current evidence."
        )
    return refreshed, {
        "status": outcome_status,
        "reason": reason,
        "attempted_at_utc": retrieved,
        "confirmed_count": confirmed_count,
        "failed_count": failed_count,
    }


def history_key(row: dict) -> tuple[str, str, str, str]:
    return (row["date"], row["brand"], row["model"], row["retailer"])


def format_amount(value) -> str:
    if value in (None, ""):
        return ""
    amount = float(value)
    text = f"{amount:.2f}"
    return text.rstrip("0").rstrip(".")


def append_history(
    listings: list[dict],
    path: Path = HISTORY_PATH,
    attempted_at: str | datetime | None = None,
) -> int:
    attempt = parse_utc(attempted_at or utc_now())
    assert attempt is not None
    attempt_iso = utc_iso(attempt)
    today = attempt.astimezone(CENTRAL_ZONE).date().isoformat()
    existing = set()
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add(history_key(row))
    else:
        path.write_text(",".join(HISTORY_FIELDS) + "\n", encoding="utf-8")

    rows_to_append = []
    for item in listings:
        if item.get("data_quality") != "confirmed":
            continue
        try:
            retrieved = utc_iso(item.get("retrieved"))
        except ValueError:
            continue
        if retrieved != attempt_iso:
            continue
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


def run_refresh(
    *,
    listings_path: Path = LISTINGS_PATH,
    specs_path: Path = SPECS_PATH,
    status_path: Path = STATUS_PATH,
    history_path: Path = HISTORY_PATH,
    now: datetime | None = None,
    offline: bool = False,
) -> dict:
    """Execute one attempt and persist truth according to its outcome."""
    now = now or utc_now()
    listings = load_json(Path(listings_path))
    specs = normalize_specs(load_json(Path(specs_path)))
    status = load_json(Path(status_path))
    try:
        candidate, outcome = refresh_listings(listings, specs, now=now, offline=offline)
    except Exception as exc:
        candidate = []
        outcome = {
            "status": "failed",
            "reason": f"Acquisition failed: {type(exc).__name__}: {exc}",
            "attempted_at_utc": iso_z(now),
            "confirmed_count": 0,
            "failed_count": len(listings),
        }

    final_listings, final_status, succeeded = apply_refresh_outcome(
        listings,
        status,
        candidate,
        outcome,
        now=now,
    )
    write_json(Path(status_path), final_status)
    appended = 0
    if succeeded:
        write_json(Path(listings_path), final_listings)
        appended = append_history(
            final_listings,
            Path(history_path),
            outcome["attempted_at_utc"],
        )
    return {**outcome, "history_appended": appended}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--outcome-path", type=Path)
    args = parser.parse_args()
    offline = args.offline or os.environ.get("KEG_TRACKER_OFFLINE") == "1"
    result = run_refresh(now=utc_now(), offline=offline)
    if args.outcome_path:
        write_json(args.outcome_path, result)
    print(
        f"refresh {result['status']}: {result['confirmed_count']} confirmed, "
        f"{result['failed_count']} failed; appended {result['history_appended']} history rows"
    )
    if offline:
        print("offline mode: live fetch skipped")


if __name__ == "__main__":
    main()
