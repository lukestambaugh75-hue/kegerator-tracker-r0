from __future__ import annotations

import csv
import copy
import hashlib
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


CANONICAL_DASHBOARD_URL = "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/"
CURRENT_SOURCE_URL = "https://www.homedepot.com/s/K309B-1"


def boundary_fixture(extra_head: str = "", extra_body: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <style>body {{ background-image: url("assets/kegerator-hero.png"); }}</style>
    {extra_head}
  </head>
  <body>
    <main><a href="{CURRENT_SOURCE_URL}">Kegco K309B-1 at Home Depot</a></main>
    {extra_body}
    <script>
      fetch("data/listings.json");
      fetch("data/specs.json");
      fetch("data/refresh-status.json");
      fetch("history.csv");
    </script>
  </body>
</html>"""


def current_listing_urls() -> frozenset[str]:
    from scripts.audience_guard import listing_source_urls

    listings = json.loads(Path("data/listings.json").read_text(encoding="utf-8"))
    return listing_source_urls(listings)


def test_compute_garage_suitability_priority_order():
    from scripts.refresh import compute_garage_suitability

    assert compute_garage_suitability({"outdoor_rated": True, "temp_low_f": 40}) == "Best - outdoor rated"
    assert compute_garage_suitability({"deep_chill": True, "fan_forced": True, "temp_low_f": 34}) == "Good - deep-chill + fan-forced"
    assert compute_garage_suitability({"deep_chill": False, "fan_forced": True, "temp_low_f": 32}) == "Good - low-30s headroom"
    assert compute_garage_suitability({"deep_chill": False, "fan_forced": False, "temp_low_f": 36}) == "Fair - limited cold headroom"


def test_normalize_listing_computes_discount_and_spec_fields():
    from scripts.refresh import normalize_listing

    spec = {
        "brand": "Kegco",
        "model": "K309B-1",
        "tap_count": 1,
        "finish": "black",
        "type": "kegerator",
        "complete_kit": True,
        "outdoor_rated": False,
        "deep_chill": True,
        "fan_forced": True,
        "temp_low_f": 32,
    }
    row = normalize_listing(
        {
            "brand": "Kegco",
            "model": "K309B-1",
            "description": "single tap",
            "retailer": "Kegco.com",
            "current_price": 914.55,
            "list_price": 961,
            "source_url": "https://kegco.com/search?q=K309B-1",
            "data_quality": "confirmed",
        },
        {"Kegco::K309B-1": spec},
        "2026-07-04T12:00:00Z",
    )

    assert row["tap_count"] == 1
    assert row["complete_kit"] is True
    assert row["discount_pct"] == 4.83
    assert row["garage_suitability"] == "Good - deep-chill + fan-forced"


def test_append_history_uses_only_exact_attempt_confirmations_and_central_date(tmp_path):
    from scripts.refresh import append_history

    history_path = tmp_path / "history.csv"
    history_path.write_text("date,brand,model,retailer,price,list_price,source,data_quality\n", encoding="utf-8")
    listings = [
        {
            "brand": "Kegco",
            "model": "K309B-1",
            "retailer": "Kegco.com",
            "current_price": 914.55,
            "list_price": 961,
            "source_url": "https://kegco.com/search?q=K309B-1",
            "data_quality": "confirmed",
            "retrieved": "2026-07-05T01:30:00Z",
        },
        {
            "brand": "Kegco",
            "model": "K309SS-1",
            "retailer": "Kegco.com",
            "current_price": 999,
            "list_price": 1099,
            "source_url": "https://kegco.com/search?q=K309SS-1",
            "data_quality": "confirmed",
            "retrieved": "2026-07-04T12:00:00Z",
        },
        {
            "brand": "Kegco",
            "model": "K309X-1",
            "retailer": "Kegco.com",
            "current_price": 879,
            "list_price": None,
            "source_url": "https://kegco.com/search?q=K309X-1",
            "data_quality": "estimated",
            "retrieved": "2026-07-05T01:30:00Z",
        }
    ]

    attempt = "2026-07-05T01:30:00Z"  # Jul 4 in America/Chicago.
    assert append_history(listings, history_path, attempt) == 1
    assert append_history(listings, history_path, attempt) == 0

    rows = list(csv.DictReader(io.StringIO(history_path.read_text(encoding="utf-8"))))
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-07-04"
    assert rows[0]["price"] == "914.55"


def test_seed_data_covers_requested_models_and_confirmed_rows():
    specs = json.loads(Path("data/specs.json").read_text(encoding="utf-8"))
    listings = json.loads(Path("data/listings.json").read_text(encoding="utf-8"))
    models = {row["model"] for row in specs}
    required = {
        "K309B-1",
        "K309SS-1",
        "K309X-1",
        "K309B-2",
        "K309SS-2",
        "K309X-2",
        "K209SS-1",
        "HBK209S-2",
        "Z163S-2",
        "HK38BSU-2",
        "HK38SSU-2",
        "HK38BSC-2",
        "HK38SSC-L-2",
        "KC2000",
        "KC2000SS",
        "KC2000TWIN",
        "KC2000SSTWIN",
        "KC1000SS",
        "BR2001SS",
        "BR2001BL",
        "BR3002BL",
        "BR7001SSOD",
        "KC7000SSOD",
        "EFRB150-SS",
        "EFRB200",
        "DKC054A1BSLDB",
        "DKC054A1BSL2DB",
        "SBC683OSTWIN",
        "Single Tap",
        "Dual Tap",
    }
    assert required.issubset(models)
    assert any(
        row["model"] == "K309SS-2"
        and row["retailer"] == "Home Depot"
        and row["source_url"] == "https://www.homedepot.com/s/K309SS-2"
        and float(row["current_price"]) > 0
        for row in listings
    )
    assert any(
        row["model"] == "BR7001SSOD"
        and row["retailer"] == "EdgeStar.com"
        and row["source_url"] == "https://www.edgestar.com/search?q=BR7001SSOD"
        and float(row["current_price"]) > 0
        for row in listings
    )


def test_dashboard_fetches_live_json_and_csv():
    html = Path("index.html").read_text(encoding="utf-8")

    assert "data/listings.json" in html
    assert "data/specs.json" in html
    assert "data/refresh-status.json" in html
    assert "history.csv" in html
    assert "Last successful data refresh" in html
    assert "historical" in html.lower()
    assert "Garage-ready" in html
    assert "Cross-retailer spread" in html
    assert "tracker-nav" not in html
    assert "Deal Trackers" not in html
    assert "Main Dashboard" not in html
    assert "Color index" in html
    assert "Green" in html
    assert "Blue" in html
    assert "Amber" in html
    assert "Red" in html
    assert "information only" in html
    assert "not a recommendation" in html
    assert "https://lukestambaugh75-hue.github.io/daily-dashboards-public-safe-r0/" not in html
    assert "https://lukestambaugh75-hue.github.io/ps5-tv-deal-tracker-r0/" not in html
    assert "https://lukestambaugh75-hue.github.io/ford-raptor-tracker-r0/" not in html


def test_audience_guard_accepts_the_current_repository():
    from scripts.audience_guard import validate_repository

    validate_repository(Path.cwd())


def test_canonical_index_digest_and_path_are_pinned():
    from scripts.audience_guard import (
        CANONICAL_INDEX_PATH,
        CANONICAL_INDEX_SHA256,
        validate_html,
    )

    path = Path.cwd() / CANONICAL_INDEX_PATH
    raw = path.read_bytes()

    assert CANONICAL_INDEX_PATH == Path("index.html")
    assert hashlib.sha256(raw).hexdigest() == CANONICAL_INDEX_SHA256
    validate_html(
        raw,
        allowed_listing_urls=current_listing_urls(),
        asset_root=Path.cwd(),
        source_path=path,
    )


def test_audience_guard_rejects_any_one_byte_index_mutation():
    from scripts.audience_guard import AudienceBoundaryError, CANONICAL_INDEX_PATH, validate_html

    path = Path.cwd() / CANONICAL_INDEX_PATH
    raw = bytearray(path.read_bytes())
    offset = raw.index(b"Kegerator Tracker")
    raw[offset] = ord("k")

    with pytest.raises(AudienceBoundaryError, match="digest mismatch"):
        validate_html(
            bytes(raw),
            allowed_listing_urls=current_listing_urls(),
            asset_root=Path.cwd(),
            source_path=path,
        )


@pytest.mark.parametrize(
    "injection",
    [
        b'<script>const a=document.querySelector("a");a["hr"+"ef"]="/other-repo/";</script>',
        b'<script>document.body.innerHTML=atob("PGlmcmFtZSBzcmM9Jy9vdGhlci1yZXBvLyc+PC9pZnJhbWU+");</script>',
        b'<script>new Audio("ht"+"tps://example.com/track.mp3");</script>',
        b'<style>.x{background:u\\72l("https://example.com/shared.png")}</style>',
    ],
)
def test_obfuscated_html_script_and_css_mutations_fail_at_the_digest_boundary(injection):
    from scripts.audience_guard import AudienceBoundaryError, CANONICAL_INDEX_PATH, validate_html

    path = Path.cwd() / CANONICAL_INDEX_PATH
    raw = path.read_bytes().replace(b"</body>", injection + b"</body>", 1)

    with pytest.raises(AudienceBoundaryError, match="digest mismatch"):
        validate_html(
            raw,
            allowed_listing_urls=current_listing_urls(),
            asset_root=Path.cwd(),
            source_path=path,
        )


def test_audience_guard_rejects_a_noncanonical_index_path():
    from scripts.audience_guard import AudienceBoundaryError, CANONICAL_INDEX_PATH, validate_html

    raw = (Path.cwd() / CANONICAL_INDEX_PATH).read_bytes()
    with pytest.raises(AudienceBoundaryError, match="canonical index path"):
        validate_html(
            raw,
            allowed_listing_urls=current_listing_urls(),
            asset_root=Path.cwd(),
            source_path=Path.cwd() / "other.html",
        )


def test_canonical_index_validation_requires_exact_bytes():
    from scripts.audience_guard import AudienceBoundaryError, CANONICAL_INDEX_PATH, validate_html

    path = Path.cwd() / CANONICAL_INDEX_PATH
    with pytest.raises(AudienceBoundaryError, match="exact bytes"):
        validate_html(
            path.read_text(encoding="utf-8"),
            allowed_listing_urls=current_listing_urls(),
            asset_root=Path.cwd(),
            source_path=path,
        )


def test_audience_semantics_accept_only_the_kegerator_page_current_sources_and_runtime_files():
    from scripts.audience_guard import validate_html_semantics

    validate_html_semantics(
        boundary_fixture(extra_body=f'<a href="{CANONICAL_DASHBOARD_URL}">Kegerator Tracker</a>'),
        allowed_listing_urls={CURRENT_SOURCE_URL},
    )


@pytest.mark.parametrize(
    ("extra_head", "extra_body"),
    [
        ("", '<a href="https://lukestambaugh75-hue.github.io/ps5-tv-deal-tracker-r0/">PS5 + TV</a>'),
        ("", '<a href="https://lukestambaugh75-hue.github.io/daily-dashboards-public-safe-r0/">Main Dashboard</a>'),
        ("", '<a href="https://lukestambaugh75-hue.github.io/ford-raptor-tracker-r0/">Raptor</a>'),
        ("", '<img src="https://lukestambaugh75-hue.github.io/other-repo/assets/card.png" alt="">'),
        ("", '<img srcset="assets/kegerator-hero.png 1x, https://example.com/shared.png 2x" alt="">'),
        ('<style>@import url("https://example.com/shared.css");</style>', ""),
        ('<style>.card { background: url("https://example.com/shared.png") }</style>', ""),
        ("", '<form action="https://example.com/collect"><button>Go</button></form>'),
        ('<meta http-equiv="refresh" content="0; url=https://example.com/elsewhere">', ""),
        ("", '<button onclick="location.href=\'https://example.com/elsewhere\'">Go</button>'),
        ("", '<script>window.location.href="https://example.com/elsewhere";</script>'),
        ("", '<script>window.open("https://example.com/elsewhere");</script>'),
        ("", '<script>fetch("https://example.com/shared-data.json");</script>'),
        ("", '<script>window["fetch"]("data/listings.json");</script>'),
        ("", '<script>const request = fetch; request("data/listings.json");</script>'),
        ("", '<script>globalThis["location"].assign("/other-repo/");</script>'),
        ("", '<script>history.pushState(null, "", "/other-repo/");</script>'),
        ("", '<script>document.defaultView.location.assign("/other-repo/");</script>'),
        ("", '<script>document.querySelector("a").href="/other-repo/";</script>'),
        ("", '<script>const anchor=document.querySelector("a");anchor["href"]="/other-repo/";</script>'),
        ("", '<script>document.body.innerHTML=\'<meta http-equiv="refresh" content="0;url=/other-repo/">\';</script>'),
        ("", '<script>new WebSocket("wss://example.com/feed");</script>'),
        ("", '<script>new Audio("ht"+"tps://example.com/feed.mp3");</script>'),
        ("", '<script src="../PS5 and TV Deal Tracker r0/assets/dashboard-ui.mjs"></script>'),
        ("", '<script src="assets/shared-dashboard-ui.mjs"></script>'),
        ("", '<img src="data/private.json" alt="">'),
        ('<link rel="preload" imagesrcset="assets/kegerator-hero.png 1x, https://example.com/shared.png 2x">', ""),
        ('<style>.card { background: u\\72l("https://example.com/shared.png") }</style>', ""),
        ("", '<iframe srcdoc="&lt;a href=&quot;https://example.com&quot;&gt;x&lt;/a&gt;"></iframe>'),
        ("", '<a href="#current-listings" ping="https://example.com/collect">Current listings</a>'),
        ("", '<input type="button" value="M a i n  D a s h b o a r d">'),
        ("", '<area href="#current-listings" alt="Ford Raptor">'),
        ("", '<a href="#current-listings"><img src="assets/kegerator-hero.png" alt="PS5 + TV"></a>'),
        ("", '<span id="other-label">Main Dashboard</span><a href="#current-listings" aria-labelledby="other-label">Open</a>'),
        ("", '<span id="label-one">Main</span><span id="label-two">Dashboard</span><a href="#current-listings" aria-labelledby="label-one label-two">Open</a>'),
        ("", '<span id="image-label"><img src="assets/kegerator-hero.png" alt="Main Dashboard"></span><a href="#current-listings" aria-labelledby="image-label">Open</a>'),
        ("", '<a href="#current-listings">D a i l y  D a s h b o a r d</a>'),
        ("", '<a href="#current-listings">All deal trackers</a>'),
        ("", '<a href="https://www.homedepot.com/s/not-a-current-listing">Another listing</a>'),
    ],
)
def test_audience_guard_rejects_cross_dashboard_and_evasive_runtime_paths(extra_head, extra_body):
    from scripts.audience_guard import AudienceBoundaryError, validate_html_semantics

    with pytest.raises(AudienceBoundaryError):
        validate_html_semantics(
            boundary_fixture(extra_head, extra_body),
            allowed_listing_urls={CURRENT_SOURCE_URL},
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"to": ["lukestambaugh75@gmail.com"], "cc": [], "bcc": [], "dashboard_url": CANONICAL_DASHBOARD_URL},
        {"to": ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com", "other@example.com"], "cc": [], "bcc": [], "dashboard_url": CANONICAL_DASHBOARD_URL},
        {"to": ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"], "cc": ["other@example.com"], "bcc": [], "dashboard_url": CANONICAL_DASHBOARD_URL},
        {"to": ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"], "cc": [], "bcc": ["other@example.com"], "dashboard_url": CANONICAL_DASHBOARD_URL},
        {"to": ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"], "cc": [], "bcc": [], "dashboard_url": "https://example.com/shared-dashboard"},
    ],
)
def test_audience_guard_rejects_wrong_recipients_or_dashboard(payload):
    from scripts.audience_guard import AudienceBoundaryError, validate_email_payload

    with pytest.raises(AudienceBoundaryError):
        validate_email_payload(payload)


def test_audience_guard_rejects_cross_dashboard_urls_hidden_in_email_bodies():
    from scripts.audience_guard import AudienceBoundaryError, validate_email_payload

    payload = {
        "to": ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"],
        "cc": [],
        "bcc": [],
        "dashboard_url": CANONICAL_DASHBOARD_URL,
        "body_text": f"Dashboard: {CANONICAL_DASHBOARD_URL}",
        "body_html": (
            f'<a href="{CANONICAL_DASHBOARD_URL}">Kegerator</a>'
            '<img src="https://lukestambaugh75-hue.github.io/ps5-tv-deal-tracker-r0/card.png">'
        ),
    }

    with pytest.raises(AudienceBoundaryError):
        validate_email_payload(payload)


def test_public_page_checker_applies_the_same_audience_boundary():
    from scripts.audience_guard import AudienceBoundaryError, CANONICAL_INDEX_PATH
    from scripts.check_public_pages import validate_public_body

    raw = (Path.cwd() / CANONICAL_INDEX_PATH).read_bytes()
    status = json.loads(Path("data/refresh-status.json").read_text(encoding="utf-8"))
    validate_public_body(raw, current_listing_urls(), status)

    bad = raw.replace(
        b"</body>",
        b'<a href="https://lukestambaugh75-hue.github.io/ps5-tv-deal-tracker-r0/">TV deals</a></body>',
        1,
    )
    with pytest.raises(AudienceBoundaryError, match="digest mismatch"):
        validate_public_body(bad, current_listing_urls(), status)


def test_email_payload_has_exact_recipients():
    from tools.build_email import build_payload

    listings = [
        {
            "brand": "Kegco",
            "model": "K309B-1",
            "description": "single tap",
            "tap_count": 1,
            "complete_kit": True,
            "outdoor_rated": False,
            "retailer": "Home Depot",
            "current_price": 843.99,
            "garage_suitability": "Good - deep-chill + fan-forced",
            "source_url": "https://www.homedepot.com/s/K309B-1",
            "data_quality": "confirmed",
        }
    ]
    refresh = {
        "data_refreshed_at_utc": "2026-07-10T12:00:00Z",
        "last_attempt_at_utc": "2026-07-10T12:00:00Z",
        "last_attempt_status": "success",
        "last_attempt_reason": None,
        "cadence_minutes": 1440,
        "grace_minutes": 180,
        "timezone": "America/Chicago",
        "archived": False,
        "source_count": 1,
        "row_count": 1,
        "quality_counts": {"verified": 1, "estimated": 0, "blocked": 0},
        "rendered_at_utc": None,
        "published_at_utc": None,
    }
    payload = build_payload(
        listings,
        [],
        refresh,
        CANONICAL_DASHBOARD_URL,
        now=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    )

    assert payload["to"] == ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"]
    assert payload["cc"] == []
    assert payload["bcc"] == []
    assert CANONICAL_DASHBOARD_URL in payload["body_text"]

    with pytest.raises(ValueError):
        build_payload(
            listings,
            [],
            refresh,
            "https://example.com/keg",
            now=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        )


def test_email_automation_mirror_uses_browser_gmail_route():
    text = Path("automation/kegerator-tracker-email.toml").read_text(encoding="utf-8")

    assert "lukestambaugh75@gmail.com" in text
    assert "devin.mullen89@gmail.com" in text
    assert "no CC/BCC" in text or "Do not add CC or BCC" in text
    assert "out/latest-email.json" in text
    assert "signed-in Chrome/Gmail browser route" in text
    assert "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/" in text


def refresh_fixture(
    *,
    success_at: str | None = "2026-07-04T12:00:00Z",
    attempt_at: str | None = "2026-07-04T12:00:00Z",
    attempt_status: str = "success",
    attempt_reason: str | None = None,
    count: int = 2,
) -> dict:
    return {
        "data_refreshed_at_utc": success_at,
        "last_attempt_at_utc": attempt_at,
        "last_attempt_status": attempt_status,
        "last_attempt_reason": attempt_reason,
        "cadence_minutes": 1440,
        "grace_minutes": 180,
        "timezone": "America/Chicago",
        "archived": False,
        "source_count": count,
        "row_count": count,
        "quality_counts": {"verified": count, "estimated": 0, "blocked": 0},
        "rendered_at_utc": None,
        "published_at_utc": None,
    }


def listing_fixture(model: str, price: float, retrieved: str = "2026-07-04T12:00:00Z") -> dict:
    return {
        "brand": "Kegco",
        "model": model,
        "description": f"{model} complete kegerator",
        "tap_count": 1,
        "finish": "black",
        "type": "kegerator",
        "complete_kit": True,
        "retailer": "Kegco.com",
        "current_price": price,
        "list_price": price + 100,
        "discount_pct": 10,
        "in_stock": True,
        "garage_suitability": "Good - deep-chill + fan-forced",
        "outdoor_rated": False,
        "source_url": f"https://kegco.com/products/{model}",
        "data_quality": "confirmed",
        "retrieved": retrieved,
    }


def jsonld_page(*nodes: dict) -> str:
    payload = nodes[0] if len(nodes) == 1 else {"@context": "https://schema.org", "@graph": list(nodes)}
    return f'<script type="application/ld+json">{json.dumps(payload)}</script>'


def test_refresh_state_precedence_boundaries_and_central_dst_labels():
    from scripts.refresh_state import evaluate_refresh, format_central

    success = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    status = refresh_fixture()

    assert evaluate_refresh(status, now=success + timedelta(minutes=1440))["state"] == "Fresh"
    assert evaluate_refresh(status, now=success + timedelta(minutes=1440, seconds=1))["state"] == "Due"
    assert evaluate_refresh(status, now=success + timedelta(minutes=1620))["state"] == "Due"
    assert evaluate_refresh(status, now=success + timedelta(minutes=1620, seconds=1))["state"] == "Stale"

    blocked = refresh_fixture(
        attempt_at="2026-07-04T13:00:00Z",
        attempt_status="partial",
        attempt_reason="1 of 2 targets confirmed.",
    )
    assert evaluate_refresh(blocked, now=success + timedelta(hours=2))["state"] == "Blocked"

    equal_attempt = refresh_fixture(attempt_status="failed", attempt_reason="same-time record")
    with pytest.raises(ValueError, match="strictly newer"):
        evaluate_refresh(equal_attempt, now=success)

    archived = copy.deepcopy(blocked)
    archived["archived"] = True
    assert evaluate_refresh(archived, now=success + timedelta(hours=2))["state"] == "Archived"

    unknown = refresh_fixture(success_at=None, attempt_at=None, attempt_status="unknown", count=0)
    assert evaluate_refresh(unknown, now=success)["state"] == "Unknown"
    assert "CDT" in format_central("2026-07-04T12:00:00Z")
    assert "CST" in format_central("2026-01-04T12:00:00Z")


def test_refresh_outcomes_classify_zero_partial_and_complete_current_evidence(monkeypatch):
    from scripts import refresh

    attempt = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    rows = [listing_fixture("ONE", 800), listing_fixture("TWO", 900)]
    specs = [
        {
            "brand": "Kegco",
            "model": row["model"],
            "tap_count": 1,
            "complete_kit": True,
            "outdoor_rated": False,
            "deep_chill": True,
            "fan_forced": True,
            "temp_low_f": 32,
        }
        for row in rows
    ]

    monkeypatch.setattr(refresh, "try_live_price", lambda row, offline: (None, "blocked_or_no_price"))
    blocked_rows, blocked = refresh.refresh_listings(rows, specs, now=attempt)
    assert blocked["status"] == "blocked"
    assert blocked["confirmed_count"] == 0
    assert blocked["failed_count"] == 2
    assert blocked["expected_count"] == 2
    assert all(row["data_quality"] == "blocked" for row in blocked_rows)

    monkeypatch.setattr(
        refresh,
        "try_live_price",
        lambda row, offline: (777.0, "parsed") if row["model"] == "ONE" else (None, "blocked_or_no_price"),
    )
    partial_rows, partial = refresh.refresh_listings(rows, specs, now=attempt)
    assert partial["status"] == "partial"
    assert partial["confirmed_count"] == 1
    assert partial["failed_count"] == 1
    assert partial["expected_count"] == 2
    assert sum(row["data_quality"] == "confirmed" for row in partial_rows) == 1

    monkeypatch.setattr(refresh, "try_live_price", lambda row, offline: (777.0, "parsed"))
    complete_rows, complete = refresh.refresh_listings(rows, specs, now=attempt)
    assert complete["status"] == "success"
    assert complete["attempted_at_utc"] == "2026-07-11T12:00:00Z"
    assert complete["confirmed_count"] == 2
    assert complete["failed_count"] == 0
    assert complete["expected_count"] == 2
    assert all(row["retrieved"] == complete["attempted_at_utc"] for row in complete_rows)


def test_stale_http_cache_is_never_promoted_to_current_confirmation(monkeypatch):
    from scripts import refresh

    calls = []

    def fake_fetch(url, use_cache=True):
        calls.append(use_cache)
        return "$123.45" if use_cache else None

    monkeypatch.setattr(refresh, "fetch_url", fake_fetch)
    row = listing_fixture("DIRECT", 800)
    row["source_url"] = "https://kegco.com/products/direct"

    amount, evidence = refresh.try_live_price(row, offline=False)

    assert amount is None
    assert evidence == "blocked_or_no_price"
    assert calls == [False]


@pytest.mark.parametrize(
    "source_url",
    [
        "https://kegco.com/search?q=K309B-1",
        "https://www.homedepot.com/s/K309B-1",
        "https://kegco.com/search-results/K309B-1",
        "https://kegco.com/browse/K309B-1",
        "https://kegco.com/searching/K309B-1",
        "https://kegco.com/categories/K309B-1",
    ],
)
def test_search_and_query_sources_are_non_confirming_without_fetch(source_url, monkeypatch):
    from scripts import refresh

    def forbidden_fetch(*args, **kwargs):
        raise AssertionError("search paths must never be fetched as confirming evidence")

    monkeypatch.setattr(refresh, "fetch_url", forbidden_fetch)
    row = listing_fixture("K309B-1", 800)
    row["source_url"] = source_url

    amount, evidence = refresh.try_live_price(row, offline=False)

    assert amount is None
    assert evidence == "search_url_skipped"


def test_direct_page_requires_matched_structured_product_offer(monkeypatch):
    from scripts import refresh

    row = listing_fixture("K309B-1", 800)
    row["source_url"] = "https://kegco.com/products/k309b-1"
    monkeypatch.setattr(
        refresh,
        "fetch_url",
        lambda url, use_cache=False: ("<title>K309B-1</title>$123.45", url),
    )
    assert refresh.try_live_price(row, offline=False) == (None, "structured_product_missing")

    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "model": "K309B-1",
        "offers": {"@type": "Offer", "price": "843.99", "priceCurrency": "USD"},
    }
    monkeypatch.setattr(
        refresh,
        "fetch_url",
        lambda url, use_cache=False: (jsonld_page(product), url),
    )
    assert refresh.try_live_price(row, offline=False) == (843.99, "parsed")


def test_structured_price_stays_bound_to_matched_product_own_offer(monkeypatch):
    from scripts import refresh

    row = listing_fixture("K309B-1", 800)
    row["source_url"] = "https://kegco.com/products/k309b-1"
    accessory = {
        "@type": "Product",
        "name": "Accessory tray",
        "offers": {"@type": "Offer", "price": "199", "priceCurrency": "USD"},
    }
    outside_offer = {"@type": "Offer", "price": "149", "priceCurrency": "USD"}
    matched = {
        "@type": "Product",
        "name": "Kegco K309B-1 complete kegerator",
        "offers": {"@type": "Offer", "price": "843.99", "priceCurrency": "USD"},
    }
    page = "$99.99 unrelated text" + jsonld_page(accessory, outside_offer, matched)
    monkeypatch.setattr(refresh, "fetch_url", lambda url, use_cache=False: (page, url))

    assert refresh.try_live_price(row, offline=False) == (843.99, "parsed")


@pytest.mark.parametrize(
    ("expected", "candidate"),
    [
        ("KC2000", "KC2000TWIN"),
        ("K309B-1", "K309B-10"),
        ("K309B-1", "XXK309B-1YY"),
    ],
)
def test_structured_product_identity_match_is_alphanumeric_bounded(expected, candidate):
    from scripts.refresh import parse_structured_product_price

    product = {
        "@type": "Product",
        "model": candidate,
        "offers": {"@type": "Offer", "price": "843.99", "priceCurrency": "USD"},
    }

    assert parse_structured_product_price(jsonld_page(product), expected) is None


@pytest.mark.parametrize("identity_field", ["model", "mpn", "sku", "productID", "name"])
def test_structured_product_accepts_exact_identity_fields_and_own_usd_offer(identity_field):
    from scripts.refresh import parse_structured_product_price

    product = {
        "@type": ["Thing", "Product"],
        identity_field: "Kegco K309B-1 complete kegerator" if identity_field == "name" else "K309B-1",
        "offers": {"@type": "Offer", "price": "843.99", "priceCurrency": "USD"},
    }

    assert parse_structured_product_price(jsonld_page(product), "K309B-1") == 843.99


def test_structured_product_supports_aggregate_low_price_but_requires_usd_and_finite_positive():
    from scripts.refresh import parse_structured_product_price

    def product(offer):
        return {"@type": "Product", "sku": "K309B-1", "offers": offer}

    assert parse_structured_product_price(
        jsonld_page(product({"@type": "AggregateOffer", "lowPrice": "799.99", "priceCurrency": "USD"})),
        "K309B-1",
    ) == 799.99
    for offer in (
        {"@type": "Offer", "price": "799.99", "priceCurrency": "EUR"},
        {"@type": "Offer", "price": "799.99"},
        {"@type": "Offer", "price": "NaN", "priceCurrency": "USD"},
        {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
    ):
        assert parse_structured_product_price(jsonld_page(product(offer)), "K309B-1") is None


class FakeHttpResponse:
    def __init__(self, body: str, final_url: str):
        self._body = body.encode("utf-8")
        self._final_url = final_url

    def read(self):
        return self._body

    def geturl(self):
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.parametrize(
    "final_url",
    [
        "https://www.kegco.com/products/k309b-1",
        "https://kegco.com/products/k309b-1?source=search",
        "https://kegco.com/products/k309b-1?",
        "https://kegco.com/products/k309b-1#",
        "https://kegco.com/search-results/k309b-1",
        "https://kegco.com/se%61rch/k309b-1",
        "https://kegco.com/browse/k309b-1",
        "https://kegco.com/searching/k309b-1",
        "https://kegco.com/category/kegerators/k309b-1",
        "https://kegco.com/catalog/k309b-1",
    ],
)
def test_fetch_url_rejects_redirect_host_query_and_search_browse_category_paths(final_url, tmp_path, monkeypatch):
    from scripts import refresh

    requested = "https://kegco.com/products/k309b-1"
    monkeypatch.setattr(refresh, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(refresh, "robots_allowed", lambda url: True)
    monkeypatch.setattr(refresh.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        refresh.urllib.request,
        "urlopen",
        lambda request, timeout: FakeHttpResponse("safe body", final_url),
    )

    assert refresh.fetch_url(requested, use_cache=False) is None
    assert list(tmp_path.iterdir()) == []


def test_fetch_url_returns_body_and_validated_final_direct_url(tmp_path, monkeypatch):
    from scripts import refresh

    requested = "https://kegco.com/products/k309b-1"
    monkeypatch.setattr(refresh, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(refresh, "robots_allowed", lambda url: True)
    monkeypatch.setattr(refresh.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        refresh.urllib.request,
        "urlopen",
        lambda request, timeout: FakeHttpResponse("safe body", requested),
    )

    assert refresh.fetch_url(requested, use_cache=False) == ("safe body", requested)


@pytest.mark.parametrize("attempt_status", ["blocked", "partial", "failed"])
def test_unsuccessful_attempt_preserves_entire_snapshot_and_changes_attempt_only(attempt_status):
    from scripts.refresh_state import apply_refresh_outcome

    prior_rows = [listing_fixture("ONE", 800), listing_fixture("TWO", 900)]
    prior_status = refresh_fixture()
    candidate_rows = [listing_fixture("ONE", 1), listing_fixture("TWO", 2)]
    outcome = {
        "status": attempt_status,
        "reason": f"{attempt_status} source evidence",
        "attempted_at_utc": "2026-07-11T12:00:00Z",
        "expected_count": 2,
        "confirmed_count": 0 if attempt_status != "partial" else 1,
        "failed_count": 2 if attempt_status != "partial" else 1,
    }

    final_rows, final_status, succeeded = apply_refresh_outcome(
        prior_rows,
        prior_status,
        candidate_rows,
        outcome,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
    )

    assert succeeded is False
    assert final_rows == prior_rows
    assert final_status["data_refreshed_at_utc"] == prior_status["data_refreshed_at_utc"]
    assert final_status["source_count"] == prior_status["source_count"]
    assert final_status["row_count"] == prior_status["row_count"]
    assert final_status["quality_counts"] == prior_status["quality_counts"]
    assert final_status["last_attempt_at_utc"] == outcome["attempted_at_utc"]
    assert final_status["last_attempt_status"] == attempt_status


def test_only_complete_success_replaces_snapshot_and_advances_success_time():
    from scripts.refresh_state import apply_refresh_outcome

    prior_rows = [listing_fixture("ONE", 800), listing_fixture("TWO", 900)]
    attempted_at = "2026-07-11T12:00:00Z"
    current = [
        listing_fixture("ONE", 700, attempted_at),
        listing_fixture("TWO", 750, attempted_at),
    ]
    outcome = {
        "status": "success",
        "reason": "2 of 2 targets confirmed from current evidence.",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": 2,
        "failed_count": 0,
    }

    final_rows, final_status, succeeded = apply_refresh_outcome(
        prior_rows,
        refresh_fixture(),
        current,
        outcome,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
    )

    assert succeeded is True
    assert final_rows == current
    assert final_status["data_refreshed_at_utc"] == attempted_at
    assert final_status["last_attempt_at_utc"] == attempted_at
    assert final_status["last_attempt_status"] == "success"
    assert final_status["last_attempt_reason"] is None
    assert final_status["quality_counts"] == {"verified": 2, "estimated": 0, "blocked": 0}


def test_complete_success_accepts_reordered_exact_stable_identity_set():
    from scripts.refresh_state import apply_refresh_outcome

    prior = [listing_fixture("ONE", 800), listing_fixture("TWO", 900)]
    attempted_at = "2026-07-11T12:00:00Z"
    candidate = [
        listing_fixture("TWO", 750, attempted_at),
        listing_fixture("ONE", 700, attempted_at),
    ]
    outcome = {
        "status": "success",
        "reason": "2 of 2 targets confirmed from current evidence.",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": 2,
        "failed_count": 0,
    }

    final_rows, _, succeeded = apply_refresh_outcome(
        prior,
        refresh_fixture(),
        candidate,
        outcome,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
    )

    assert succeeded is True
    assert final_rows == candidate


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "substituted"])
def test_complete_success_rejects_duplicate_missing_or_substituted_target_identity(mutation):
    from scripts.refresh_state import apply_refresh_outcome

    prior = [listing_fixture("ONE", 800), listing_fixture("TWO", 900)]
    attempted_at = "2026-07-11T12:00:00Z"
    candidate = [
        listing_fixture("ONE", 700, attempted_at),
        listing_fixture("TWO", 750, attempted_at),
    ]
    if mutation == "duplicate":
        candidate[1] = copy.deepcopy(candidate[0])
    elif mutation == "missing":
        candidate = candidate[:1]
    else:
        candidate[1]["source_url"] = "https://kegco.com/products/substituted"
    outcome = {
        "status": "success",
        "reason": "2 of 2 targets confirmed from current evidence.",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": 2,
        "failed_count": 0,
    }

    with pytest.raises(ValueError, match="identity|every target"):
        apply_refresh_outcome(
            prior,
            refresh_fixture(),
            candidate,
            outcome,
            now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("current_price", 0),
        ("current_price", -1),
        ("current_price", float("nan")),
        ("current_price", float("inf")),
        ("list_price", float("-inf")),
    ],
)
def test_complete_success_rejects_non_finite_or_non_positive_prices(field, value):
    from scripts.refresh_state import apply_refresh_outcome

    attempted_at = "2026-07-11T12:00:00Z"
    candidate = [
        listing_fixture("ONE", 700, attempted_at),
        listing_fixture("TWO", 750, attempted_at),
    ]
    candidate[0][field] = value
    outcome = {
        "status": "success",
        "reason": "2 of 2 targets confirmed from current evidence.",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": 2,
        "failed_count": 0,
    }

    with pytest.raises(ValueError, match="finite positive"):
        apply_refresh_outcome(
            [listing_fixture("ONE", 800), listing_fixture("TWO", 900)],
            refresh_fixture(),
            candidate,
            outcome,
            now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("outcome_status", "confirmed", "failed"),
    [
        ("success", 1, 1),
        ("blocked", 1, 1),
        ("partial", 0, 2),
        ("partial", 2, 0),
        ("failed", 1, 1),
    ],
)
def test_refresh_outcome_counts_fail_closed_by_status(outcome_status, confirmed, failed):
    from scripts.refresh_state import apply_refresh_outcome

    attempted_at = "2026-07-11T12:00:00Z"
    candidate = [
        listing_fixture("ONE", 700, attempted_at),
        listing_fixture("TWO", 750, attempted_at),
    ]
    outcome = {
        "status": outcome_status,
        "reason": "test outcome",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": confirmed,
        "failed_count": failed,
    }

    with pytest.raises(ValueError, match="requires"):
        apply_refresh_outcome(
            [listing_fixture("ONE", 800), listing_fixture("TWO", 900)],
            refresh_fixture(),
            candidate,
            outcome,
            now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        )


def test_apply_refresh_requires_positive_consistent_expected_count_and_unique_prior_targets():
    from scripts.refresh_state import apply_refresh_outcome

    attempted_at = "2026-07-11T12:00:00Z"
    outcome = {
        "status": "blocked",
        "reason": "no evidence",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": 0,
        "failed_count": 2,
    }
    inconsistent = refresh_fixture()
    inconsistent["source_count"] = 3
    with pytest.raises(ValueError, match="source_count.*row_count"):
        apply_refresh_outcome(
            [listing_fixture("ONE", 800), listing_fixture("TWO", 900)],
            inconsistent,
            [],
            outcome,
            now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        )

    duplicate_prior = [listing_fixture("ONE", 800), listing_fixture("ONE", 900)]
    with pytest.raises(ValueError, match="duplicate stable identity"):
        apply_refresh_outcome(
            duplicate_prior,
            refresh_fixture(),
            [],
            outcome,
            now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        )

    for invalid_expected_count in (None, 0, 3, "2"):
        invalid = copy.deepcopy(outcome)
        if invalid_expected_count is None:
            invalid.pop("expected_count")
        else:
            invalid["expected_count"] = invalid_expected_count
        with pytest.raises(ValueError, match="expected_count"):
            apply_refresh_outcome(
                [listing_fixture("ONE", 800), listing_fixture("TWO", 900)],
                refresh_fixture(),
                [],
                invalid,
                now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
            )


@pytest.mark.parametrize(
    ("attempted_at", "now", "message"),
    [
        ("2026-07-04T11:59:59Z", "2026-07-11T12:00:00Z", "newer than the stored attempt"),
        ("2026-07-11T12:00:01Z", "2026-07-11T12:00:00Z", "future"),
    ],
)
def test_attempts_reject_older_and_future_evidence(attempted_at, now, message):
    from scripts.refresh_state import apply_refresh_outcome

    outcome = {
        "status": "blocked",
        "reason": "no current evidence",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": 0,
        "failed_count": 2,
    }
    with pytest.raises(ValueError, match=message):
        apply_refresh_outcome(
            [listing_fixture("ONE", 800), listing_fixture("TWO", 900)],
            refresh_fixture(),
            [],
            outcome,
            now=datetime.fromisoformat(now.replace("Z", "+00:00")),
        )


def test_run_refresh_persists_only_attempt_metadata_when_blocked(tmp_path, monkeypatch):
    from scripts import refresh

    listings_path = tmp_path / "listings.json"
    specs_path = tmp_path / "specs.json"
    status_path = tmp_path / "refresh-status.json"
    history_path = tmp_path / "history.csv"
    original_rows = [listing_fixture("ONE", 800), listing_fixture("TWO", 900)]
    listings_path.write_text(json.dumps(original_rows) + "\n", encoding="utf-8")
    specs_path.write_text("[]\n", encoding="utf-8")
    status_path.write_text(json.dumps(refresh_fixture()) + "\n", encoding="utf-8")
    history_path.write_text(
        "date,brand,model,retailer,price,list_price,source,data_quality\n",
        encoding="utf-8",
    )
    attempted_at = "2026-07-11T12:00:00Z"
    candidate = [listing_fixture("ONE", 1), listing_fixture("TWO", 2)]
    outcome = {
        "status": "blocked",
        "reason": "0 of 2 targets were confirmed.",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": 0,
        "failed_count": 2,
    }
    monkeypatch.setattr(refresh, "refresh_listings", lambda *args, **kwargs: (candidate, outcome))

    result = refresh.run_refresh(
        listings_path=listings_path,
        specs_path=specs_path,
        status_path=status_path,
        history_path=history_path,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked"
    assert json.loads(listings_path.read_text(encoding="utf-8")) == original_rows
    assert history_path.read_text(encoding="utf-8").count("\n") == 1
    stored_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert stored_status["data_refreshed_at_utc"] == "2026-07-04T12:00:00Z"
    assert stored_status["last_attempt_at_utc"] == attempted_at


def test_run_refresh_writes_snapshot_and_exact_attempt_history_only_on_success(tmp_path, monkeypatch):
    from scripts import refresh

    listings_path = tmp_path / "listings.json"
    specs_path = tmp_path / "specs.json"
    status_path = tmp_path / "refresh-status.json"
    history_path = tmp_path / "history.csv"
    listings_path.write_text(
        json.dumps([listing_fixture("ONE", 800), listing_fixture("TWO", 900)]) + "\n",
        encoding="utf-8",
    )
    specs_path.write_text("[]\n", encoding="utf-8")
    status_path.write_text(json.dumps(refresh_fixture()) + "\n", encoding="utf-8")
    history_path.write_text(
        "date,brand,model,retailer,price,list_price,source,data_quality\n",
        encoding="utf-8",
    )
    attempted_at = "2026-07-11T12:00:00Z"
    candidate = [
        listing_fixture("ONE", 700, attempted_at),
        listing_fixture("TWO", 750, attempted_at),
    ]
    outcome = {
        "status": "success",
        "reason": "2 of 2 targets confirmed from current evidence.",
        "attempted_at_utc": attempted_at,
        "expected_count": 2,
        "confirmed_count": 2,
        "failed_count": 0,
    }
    monkeypatch.setattr(refresh, "refresh_listings", lambda *args, **kwargs: (candidate, outcome))

    result = refresh.run_refresh(
        listings_path=listings_path,
        specs_path=specs_path,
        status_path=status_path,
        history_path=history_path,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "success"
    assert result["history_appended"] == 2
    assert json.loads(listings_path.read_text(encoding="utf-8")) == candidate
    stored_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert stored_status["data_refreshed_at_utc"] == attempted_at
    history_rows = list(csv.DictReader(io.StringIO(history_path.read_text(encoding="utf-8"))))
    assert len(history_rows) == 2
    assert {row["date"] for row in history_rows} == {"2026-07-11"}


def test_history_repair_check_write_idempotence_and_validation(tmp_path):
    from scripts.repair_history import repair_history

    path = tmp_path / "history.csv"
    header = "date,brand,model,retailer,price,list_price,source,data_quality\n"
    confirmed = "2026-07-04,Kegco,A,Kegco.com,800,900,https://kegco.com/a,confirmed\n"
    estimated = "2026-07-05,Kegco,A,Kegco.com,800,900,https://kegco.com/a,estimated\n"
    original = header + confirmed * 24 + estimated * 144
    path.write_text(original, encoding="utf-8")

    kept, removed = repair_history(path, check=True)
    assert (kept, removed) == (24, 144)
    assert path.read_text(encoding="utf-8") == original

    kept, removed = repair_history(path)
    assert (kept, removed) == (24, 144)
    assert path.read_bytes() == (header + confirmed * 24).encode("utf-8")
    assert repair_history(path, check=True) == (24, 0)
    assert repair_history(path) == (24, 0)

    bad = tmp_path / "bad.csv"
    bad.write_text(header + confirmed.replace("confirmed", "mystery"), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown data_quality"):
        repair_history(bad, check=True)


@pytest.mark.parametrize(
    ("price", "list_price"),
    [
        ("NaN", "900"),
        ("Inf", "900"),
        ("-Inf", "900"),
        ("800", "NaN"),
        ("800", "Inf"),
    ],
)
def test_history_repair_rejects_non_finite_prices(tmp_path, price, list_price):
    from scripts.repair_history import repair_history

    path = tmp_path / "history.csv"
    path.write_text(
        "date,brand,model,retailer,price,list_price,source,data_quality\n"
        f"2026-07-04,Kegco,A,Kegco.com,{price},{list_price},https://kegco.com/a,confirmed\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid price"):
        repair_history(path, check=True)


def test_history_repair_cli_reports_exact_check_counts_and_exit_codes(tmp_path):
    path = tmp_path / "history.csv"
    header = "date,brand,model,retailer,price,list_price,source,data_quality\n"
    confirmed = "2026-07-04,Kegco,A,Kegco.com,800,900,https://kegco.com/a,confirmed\n"
    estimated = "2026-07-05,Kegco,A,Kegco.com,800,900,https://kegco.com/a,estimated\n"
    path.write_text(header + confirmed * 24 + estimated * 144, encoding="utf-8")
    script = Path("scripts/repair_history.py").resolve()

    before = subprocess.run(
        [sys.executable, str(script), "--path", str(path), "--check"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert before.returncode != 0
    assert before.stdout.strip() == "24 kept, 144 would remove"

    repaired = subprocess.run(
        [sys.executable, str(script), "--path", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert repaired.returncode == 0
    assert repaired.stdout.strip() == "24 kept, 144 removed"

    after = subprocess.run(
        [sys.executable, str(script), "--path", str(path), "--check"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert after.returncode == 0
    assert after.stdout.strip() == "24 kept, 0 would remove"


def test_checked_in_snapshot_and_history_remain_success_only():
    from scripts.refresh_state import validate_refresh_status

    listings = json.loads(Path("data/listings.json").read_text(encoding="utf-8"))
    status = validate_refresh_status(
        json.loads(Path("data/refresh-status.json").read_text(encoding="utf-8"))
    )
    history_rows = list(csv.DictReader(Path("history.csv").open(newline="", encoding="utf-8")))

    assert len(listings) == 24
    assert {row["data_quality"] for row in listings} == {"confirmed"}
    assert {row["retrieved"] for row in listings} == {status["data_refreshed_at_utc"]}
    assert status["source_count"] == len(listings)
    assert status["row_count"] == len(listings)
    assert status["quality_counts"] == {"verified": 24, "estimated": 0, "blocked": 0}
    assert len(history_rows) >= 24
    assert {row["data_quality"] for row in history_rows} == {"confirmed"}


def test_migration_changes_only_listing_provenance_not_prices_urls_or_timestamps():
    from scripts.refresh_state import migrate_successful_snapshot

    previous = [
        {**listing_fixture("ONE", 843.99), "data_quality": "estimated"},
        {**listing_fixture("TWO", 999.99), "data_quality": "estimated"},
    ]
    original = copy.deepcopy(previous)
    current = migrate_successful_snapshot(previous, "2026-07-04T12:00:00Z")

    def without_quality(rows):
        return [
            {key: value for key, value in row.items() if key != "data_quality"}
            for row in rows
        ]

    assert without_quality(current) == without_quality(previous)
    assert previous == original
    assert {row["data_quality"] for row in previous} == {"estimated"}
    assert {row["data_quality"] for row in current} == {"confirmed"}


@pytest.mark.parametrize(
    ("status", "now", "expected_state"),
    [
        (
            refresh_fixture(
                attempt_at="2026-07-10T13:10:23Z",
                attempt_status="blocked",
                attempt_reason="0 of 2 targets confirmed.",
            ),
            datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc),
            "Blocked",
        ),
        (
            refresh_fixture(),
            datetime(2026, 7, 5, 15, 0, 1, tzinfo=timezone.utc),
            "Stale",
        ),
        (
            refresh_fixture(success_at=None, attempt_at=None, attempt_status="unknown", count=0),
            datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc),
            "Unknown",
        ),
    ],
)
def test_non_actionable_email_contains_no_prices_or_recommendation_language(status, now, expected_state):
    from tools.build_email import build_payload

    rows = [listing_fixture("ONE", 843.99), listing_fixture("TWO", 999.99)]
    payload = build_payload(rows, [], status, CANONICAL_DASHBOARD_URL, now=now)
    combined = f"{payload['subject']}\n{payload['body_text']}\n{payload['body_html']}"
    lowered = combined.lower()

    assert payload["refresh_state"] == expected_state
    assert "Last successful data refresh:" in payload["body_text"]
    assert "Latest attempt:" in payload["body_text"]
    assert "State reason:" in payload["body_text"]
    assert CANONICAL_DASHBOARD_URL in combined
    assert "$" not in combined
    assert "lowest" not in lowered
    assert "best" not in lowered
    assert "current recommendation" not in lowered


@pytest.mark.parametrize(
    "status",
    [
        refresh_fixture(
            attempt_at="2026-07-10T13:10:23Z",
            attempt_status="blocked",
            attempt_reason="Lowest $   499 row was the best CURRENT-RECOMMENDATION.",
        ),
        refresh_fixture(
            success_at=None,
            attempt_at="2026-07-10T13:10:23Z",
            attempt_status="failed",
            attempt_reason="Lowest $   499 row was the best CURRENT-RECOMMENDATION.",
            count=0,
        ),
    ],
)
def test_non_actionable_email_sanitizes_actionable_language_inside_reason(status):
    from tools.build_email import build_payload

    payload = build_payload(
        [listing_fixture("ONE", 843.99), listing_fixture("TWO", 999.99)],
        [],
        status,
        CANONICAL_DASHBOARD_URL,
        now=datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc),
    )
    combined = f"{payload['subject']}\n{payload['body_text']}\n{payload['body_html']}".lower()
    assert "$" not in combined
    assert "lowest" not in combined
    assert "best" not in combined
    assert "current recommendation" not in combined
    assert "current-recommendation" not in combined


@pytest.mark.parametrize(
    "mutation",
    [
        "success_missing_attempt",
        "success_mismatched_attempt",
        "failure_missing_attempt",
        "failure_equal_success",
        "failure_before_success",
        "unknown_with_attempt",
        "success_missing_success",
    ],
)
def test_refresh_status_rejects_contradictory_or_missing_chronology(mutation):
    from scripts.refresh_state import validate_refresh_status

    status = refresh_fixture()
    if mutation == "success_missing_attempt":
        status["last_attempt_at_utc"] = None
    elif mutation == "success_mismatched_attempt":
        status["last_attempt_at_utc"] = "2026-07-04T13:00:00Z"
    elif mutation == "failure_missing_attempt":
        status.update(last_attempt_status="blocked", last_attempt_at_utc=None, last_attempt_reason="blocked")
    elif mutation == "failure_equal_success":
        status.update(last_attempt_status="blocked", last_attempt_reason="blocked")
    elif mutation == "failure_before_success":
        status.update(
            last_attempt_status="failed",
            last_attempt_at_utc="2026-07-04T11:59:59Z",
            last_attempt_reason="failed",
        )
    elif mutation == "unknown_with_attempt":
        status.update(
            data_refreshed_at_utc=None,
            last_attempt_status="unknown",
            last_attempt_at_utc="2026-07-04T12:00:00Z",
            last_attempt_reason=None,
            source_count=0,
            row_count=0,
            quality_counts={"verified": 0, "estimated": 0, "blocked": 0},
        )
    else:
        status["data_refreshed_at_utc"] = None

    with pytest.raises(ValueError, match="attempt|successful|strictly newer|unknown"):
        validate_refresh_status(status)


def test_public_verifier_inherits_refresh_chronology_validation():
    from scripts.check_public_pages import validate_public_status

    status = refresh_fixture(
        attempt_status="blocked",
        attempt_reason="blocked",
    )
    with pytest.raises(ValueError, match="strictly newer"):
        validate_public_status(
            status,
            [listing_fixture("ONE", 800), listing_fixture("TWO", 900)],
            now=datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc),
        )


def test_unknown_dashboard_uses_not_recorded_and_derived_reason_copy():
    html = Path("index.html").read_text(encoding="utf-8")

    assert 'if (!value) return "Not recorded"' in html
    assert 'refreshReason: "No successful data refresh is recorded."' in html
    assert 'document.getElementById("refreshReason").textContent = state.refreshReason' in html


def test_workflow_serializes_publishes_status_then_fails_non_success_without_double_refresh():
    workflow = Path(".github/workflows/refresh.yml").read_text(encoding="utf-8")

    assert "concurrency:" in workflow
    assert "group: kegerator-refresh" in workflow
    assert "cancel-in-progress: false" in workflow
    assert workflow.count("python scripts/refresh.py") == 1
    assert "make verify" not in workflow
    assert "make refresh" not in workflow
    assert "data/refresh-status.json" in workflow
    assert "scripts/repair_history.py --check" in workflow
    assert "tools/build_email.py" in workflow
    assert "scripts/audience_guard.py" in workflow
    assert "partial" in workflow
    commit_position = workflow.index("git commit")
    failure_position = workflow.index("Fail non-successful refresh")
    assert commit_position < failure_position
    assert "git push" in workflow[commit_position:failure_position]


def test_public_status_contract_matches_snapshot_and_historical_dashboard_treatment():
    from scripts.check_public_pages import validate_public_status

    listings = [listing_fixture("ONE", 843.99), listing_fixture("TWO", 999.99)]
    status = refresh_fixture(
        attempt_at="2026-07-10T13:10:23Z",
        attempt_status="blocked",
        attempt_reason="0 of 2 targets were confirmed.",
    )
    body = Path("index.html").read_bytes()

    state = validate_public_status(
        status,
        listings,
        now=datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc),
    )

    assert state["state"] == "Blocked"
    assert b"data_refreshed_at_utc" in body
    assert b"Last successful data refresh" in body
    assert b"Historical only" in body
    assert b"data/refresh-status.json" in body
