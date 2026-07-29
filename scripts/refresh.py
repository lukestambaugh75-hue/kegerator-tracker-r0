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
import stat
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from .refresh_state import (
        apply_refresh_outcome,
        build_refresh_target_identity,
        parse_utc,
        utc_iso,
    )
except ImportError:
    from refresh_state import (
        apply_refresh_outcome,
        build_refresh_target_identity,
        parse_utc,
        utc_iso,
    )


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


def write_json_exclusive(path: Path, data, *, dir_fd: int | None = None) -> None:
    """Create one run outcome without replacing an earlier attempt record."""
    path = Path(os.path.abspath(os.fspath(path)))
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if dir_fd is None:
        cursor = Path(path.anchor)
        for part in path.parent.parts[1:]:
            cursor = cursor / part
            mode = cursor.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"exclusive outcome path must not contain symlinks: {cursor}")
            if not stat.S_ISDIR(mode):
                raise ValueError(f"exclusive outcome parent is not a directory: {cursor}")
        fd = os.open(path, flags, 0o600)
        parent_fd = None
    else:
        parent = os.fstat(dir_fd)
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
            raise ValueError("exclusive outcome directory FD is invalid")
        fd = os.open(path.name, flags, 0o600, dir_fd=dir_fd)
        parent_fd = dir_fd
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        written = os.fstat(handle.fileno())
        if not stat.S_ISREG(written.st_mode) or written.st_nlink != 1 or written.st_uid != os.getuid():
            raise ValueError("exclusive outcome file identity is invalid")
        if parent_fd is not None:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (written.st_dev, written.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("exclusive outcome file identity changed during creation")
            os.fsync(parent_fd)


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
    segments = [segment.casefold() for segment in path.split("/") if segment]
    if not segments:
        return True
    for segment in segments:
        if segment in {"s", "c"} or segment.startswith(
            ("search", "brows", "categor", "catalog", "collection")
        ):
            return True
    return segments[-1] in {"product", "products", "item", "items", "p"}


def _url_has_ambiguous_path_syntax(raw_url: str, path: str) -> bool:
    if "%" in raw_url or "\\" in raw_url:
        return True
    if any(character.isspace() or unicodedata.category(character).startswith("C") for character in raw_url):
        return True
    if "//" in path:
        return True
    return any(segment in {".", ".."} for segment in path.split("/"))


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
        and not _url_has_ambiguous_path_syntax(raw_url, parsed.path)
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


class _MicrodataProducts(HTMLParser):
    """Collect Product/Offer microdata without borrowing unrelated prices."""

    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: list[dict] = []
        self._depth = 0
        self._product: dict | None = None
        self._product_depth: int | None = None
        self._offer: dict | None = None
        self._offer_depth: int | None = None
        self._capture: tuple[str, int, list[str]] | None = None

    @staticmethod
    def _item_type(attributes: dict[str, str]) -> str:
        return attributes.get("itemtype", "").rstrip("/").rsplit("/", 1)[-1].casefold()

    def _store_property(self, name: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        if self._offer is not None and name in {"price", "lowPrice", "priceCurrency"}:
            self._offer[name] = value
        elif self._product is not None and name in {"model", "mpn", "sku", "productID"}:
            self._product[name] = value

    def handle_starttag(self, tag, attrs) -> None:
        tag = tag.casefold()
        attributes = {str(name): str(value or "") for name, value in attrs}
        item_type = self._item_type(attributes)
        is_scope = "itemscope" in attributes
        if is_scope and item_type == "product" and self._product is None:
            self._product = {"@type": "Product", "offers": []}
            self._product_depth = self._depth
        elif is_scope and item_type in {"offer", "aggregateoffer"} and self._product is not None:
            self._offer = {"@type": "Offer" if item_type == "offer" else "AggregateOffer"}
            self._offer_depth = self._depth

        item_properties = attributes.get("itemprop", "").split()
        for name in item_properties:
            value = attributes.get("content") or attributes.get("value")
            if value:
                self._store_property(name, value)
            elif tag not in self._VOID_TAGS and name in {
                "model",
                "mpn",
                "sku",
                "productID",
                "price",
                "lowPrice",
                "priceCurrency",
            }:
                self._capture = (name, self._depth, [])
        if tag not in self._VOID_TAGS:
            self._depth += 1

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture[2].append(data)

    def handle_endtag(self, tag) -> None:
        if tag.casefold() in self._VOID_TAGS:
            return
        self._depth = max(0, self._depth - 1)
        if self._capture is not None and self._capture[1] == self._depth:
            name, _, parts = self._capture
            self._store_property(name, "".join(parts))
            self._capture = None
        if self._offer is not None and self._offer_depth == self._depth:
            assert self._product is not None
            self._product["offers"].append(self._offer)
            self._offer = None
            self._offer_depth = None
        if self._product is not None and self._product_depth == self._depth:
            self.products.append(self._product)
            self._product = None
            self._product_depth = None

    def close(self) -> None:
        """Flush a valid open Product scope when retailer HTML is imperfect."""
        super().close()
        if self._offer is not None and self._product is not None:
            self._product["offers"].append(self._offer)
            self._offer = None
            self._offer_depth = None
        if self._product is not None:
            self.products.append(self._product)
            self._product = None
            self._product_depth = None


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
    types = _schema_types(value)
    if "product" in types:
        yield value
    elif "productgroup" in types:
        variants = value.get("hasVariant")
        variants = variants if isinstance(variants, list) else [variants]
        for variant in variants:
            if isinstance(variant, dict) and isinstance(variant.get("offers"), (dict, list)):
                yield variant
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _iter_product_nodes(child)


def _normalized_dedicated_identity(value: object) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _is_kegco_manufacturer_sku(value: object, expected_model: object) -> bool:
    normalized = _normalized_dedicated_identity(value)
    expected = _normalized_dedicated_identity(expected_model)
    return bool(normalized and expected and normalized == f"mp{expected}")


def _has_exact_dedicated_identity(product: dict, expected_model: object) -> bool:
    expected = _normalized_dedicated_identity(expected_model)
    if not expected:
        return False
    exact_match = any(
        _normalized_dedicated_identity(product.get(field)) == expected
        for field in ("model", "mpn", "sku", "productID")
    )
    if exact_match:
        return True
    if _is_kegco_manufacturer_sku(product.get("sku"), expected_model):
        return True
    return False


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
    """Read USD price only from one exact JSON-LD or microdata product offer."""
    if not page_html:
        return None
    parser = _JsonLdScripts()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:
        return None
    matching_products: list[dict] = []
    for document in parser.documents:
        try:
            value = json.loads(document)
        except (TypeError, json.JSONDecodeError):
            continue
        for product in _iter_product_nodes(value):
            if _has_exact_dedicated_identity(product, expected_model):
                matching_products.append(product)
    if len(matching_products) == 1:
        prices = _own_usd_offer_prices(matching_products[0])
        if prices:
            return min(prices)

    microdata = _MicrodataProducts()
    try:
        microdata.feed(page_html)
        microdata.close()
    except Exception:
        return None
    matching_microdata = [
        product
        for product in microdata.products
        if _has_exact_dedicated_identity(product, expected_model)
    ]
    if len(matching_microdata) != 1:
        return None
    prices = _own_usd_offer_prices(matching_microdata[0])
    return min(prices) if prices else None


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
    expected_source_model = row.get("source_model") or row.get("model")
    amount = parse_structured_product_price(page_html, expected_source_model)
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
    snapshot_updated = succeeded or outcome["status"] == "partial"
    if snapshot_updated:
        write_json(Path(listings_path), final_listings)
        current_rows = [
            row
            for row in final_listings
            if row.get("data_quality") == "confirmed"
            and utc_iso(row.get("retrieved")) == utc_iso(outcome["attempted_at_utc"])
        ]
        appended = append_history(
            current_rows,
            Path(history_path),
            outcome["attempted_at_utc"],
        )
    return {
        **outcome,
        "snapshot_updated": snapshot_updated,
        "history_appended": appended,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--outcome-path", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--exclusive-outcome", action="store_true")
    args = parser.parse_args()
    if args.exclusive_outcome and (args.outcome_path is None or not args.run_id):
        parser.error("--exclusive-outcome requires --outcome-path and --run-id")
    offline = args.offline or os.environ.get("KEG_TRACKER_OFFLINE") == "1"
    target_identity = build_refresh_target_identity(load_json(LISTINGS_PATH))
    result = run_refresh(now=utc_now(), offline=offline)
    if args.outcome_path:
        outcome = {
            **result,
            "run_id": args.run_id,
            "input_source_count": target_identity["source_count"],
            "target_manifest_sha256": target_identity["target_manifest_sha256"],
        }
        if args.exclusive_outcome:
            inherited_dir_fd = os.environ.get("KEG_EVIDENCE_DIR_FD")
            write_json_exclusive(
                args.outcome_path,
                outcome,
                dir_fd=int(inherited_dir_fd) if inherited_dir_fd is not None else None,
            )
        else:
            write_json(args.outcome_path, outcome)
    print(
        f"refresh {result['status']}: {result['confirmed_count']} confirmed, "
        f"{result['failed_count']} failed; appended {result['history_appended']} history rows"
    )
    if offline:
        print("offline mode: live fetch skipped")


if __name__ == "__main__":
    main()
