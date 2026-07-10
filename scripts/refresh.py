#!/usr/bin/env python3
"""Refresh kegerator listings and append price history."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from html.parser import HTMLParser
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


def _path_has_discovery_segment(path: str) -> bool:
    segments = [urllib.parse.unquote(segment).casefold() for segment in path.split("/") if segment]
    if not segments:
        return True
    for segment in segments:
        if segment in {"s", "c"} or segment.startswith(
            ("search", "brows", "categor", "catalog", "collection")
        ):
            return True
    return segments[-1] in {"product", "products", "item", "items", "p"}


def source_is_direct_product_page(url: str) -> bool:
    raw_url = str(url or "")
    parsed = urllib.parse.urlsplit(raw_url)
    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and "?" not in raw_url
        and "#" not in raw_url
        and not _path_has_discovery_segment(parsed.path)
    )


def validate_final_response_url(requested_url: str, final_url: str) -> str:
    """Require the final response to remain on the exact direct-product host/path class."""
    requested = urllib.parse.urlsplit(str(requested_url or ""))
    final = urllib.parse.urlsplit(str(final_url or ""))
    if not source_is_direct_product_page(requested_url):
        raise ValueError("requested URL is not a direct product path")
    if not source_is_direct_product_page(final_url):
        raise ValueError("final response URL is not a direct product path")
    if final.netloc.casefold() != requested.netloc.casefold():
        raise ValueError("final response URL changed host")
    return final.geturl()


def fetch_url(url: str, use_cache: bool = False) -> tuple[str, str] | None:
    if not source_is_direct_product_page(url):
        return None
    # Cached bodies are diagnostic only and can never be current evidence.
    if use_cache:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = cache_path_for(url)
    if not robots_allowed(url):
        return None
    time.sleep(MIN_REQUEST_SECONDS)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            final_url = validate_final_response_url(url, response.geturl())
            body = response.read().decode("utf-8", errors="ignore")
    except (AttributeError, TypeError, ValueError, urllib.error.URLError, TimeoutError):
        return None
    cache_path.write_text(body, encoding="utf-8")
    return body, final_url


class _JsonLdScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag, attrs) -> None:
        if tag.casefold() != "script":
            return
        attributes = {str(name).casefold(): str(value or "") for name, value in attrs}
        media_type = attributes.get("type", "").split(";", 1)[0].strip().casefold()
        self._parts = [] if media_type == "application/ld+json" else None

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag) -> None:
        if tag.casefold() == "script" and self._parts is not None:
            self.documents.append("".join(self._parts))
            self._parts = None


def _schema_types(node: dict) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {
        str(value).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1].casefold()
        for value in values
        if isinstance(value, str)
    }


def _iter_product_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _iter_product_nodes(item)
        return
    if not isinstance(value, dict):
        return
    if "product" in _schema_types(value):
        yield value
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _iter_product_nodes(child)


def _identity_matches(value: object, expected_model: object) -> bool:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return False
    parts = re.findall(r"[a-z0-9]+", str(expected_model or "").casefold())
    if not parts:
        return False
    pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(
        re.escape(part) for part in parts
    ) + r"(?![a-z0-9])"
    return re.search(pattern, str(value), flags=re.IGNORECASE) is not None


def _finite_positive_price(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", cleaned):
            return None
        value = cleaned
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) and amount > 0 else None


def _own_usd_offer_prices(product: dict) -> list[float]:
    raw_offers = product.get("offers")
    offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
    prices: list[float] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        if not (_schema_types(offer) & {"offer", "aggregateoffer"}):
            continue
        if str(offer.get("priceCurrency") or "").strip().upper() != "USD":
            continue
        for field in ("price", "lowPrice"):
            amount = _finite_positive_price(offer.get(field))
            if amount is not None:
                prices.append(amount)
    return prices


def parse_structured_product_price(page_html: str | None, expected_model: object) -> float | None:
    """Read USD price only from the matched JSON-LD Product's own offers."""
    if not page_html:
        return None
    parser = _JsonLdScripts()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:
        return None
    candidates: list[float] = []
    for document in parser.documents:
        try:
            value = json.loads(document)
        except (TypeError, json.JSONDecodeError):
            continue
        for product in _iter_product_nodes(value):
            if not any(
                _identity_matches(product.get(field), expected_model)
                for field in ("model", "mpn", "sku", "productID", "name")
            ):
                continue
            candidates.extend(_own_usd_offer_prices(product))
    return min(candidates) if candidates else None


def try_live_price(row: dict, offline: bool) -> tuple[float | None, str]:
    if offline:
        return None, "offline"
    url = row.get("source_url") or ""
    if not source_is_direct_product_page(url):
        return None, "search_url_skipped"
    # A cache can help humans diagnose source changes, but it is never current
    # confirmation for a scheduled data attempt.
    fetched = fetch_url(url, use_cache=False)
    if fetched is None:
        return None, "blocked_or_no_price"
    page_html, final_url = fetched
    try:
        validate_final_response_url(url, final_url)
    except ValueError:
        return None, "redirect_rejected"
    amount = parse_structured_product_price(page_html, row.get("model"))
    return amount, "parsed" if amount is not None else "structured_product_missing"


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
        "expected_count": len(refreshed),
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
            "expected_count": len(listings),
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
