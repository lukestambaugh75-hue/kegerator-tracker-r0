import csv
import hashlib
import io
import json
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


def test_append_history_is_append_only_and_dedupes_same_day(tmp_path):
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
        }
    ]

    assert append_history(listings, history_path, "2026-07-04") == 1
    assert append_history(listings, history_path, "2026-07-04") == 0

    rows = list(csv.DictReader(io.StringIO(history_path.read_text(encoding="utf-8"))))
    assert len(rows) == 1
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
    assert any(row["model"] == "K309SS-2" and row["retailer"] == "Home Depot" and row["current_price"] == 668.76 for row in listings)
    assert any(row["model"] == "BR7001SSOD" and row["retailer"] == "EdgeStar.com" and row["current_price"] == 472.11 for row in listings)


def test_dashboard_fetches_live_json_and_csv():
    html = Path("index.html").read_text(encoding="utf-8")

    assert "data/listings.json" in html
    assert "data/specs.json" in html
    assert "history.csv" in html
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
    validate_public_body(raw, current_listing_urls())

    bad = raw.replace(
        b"</body>",
        b'<a href="https://lukestambaugh75-hue.github.io/ps5-tv-deal-tracker-r0/">TV deals</a></body>',
        1,
    )
    with pytest.raises(AudienceBoundaryError, match="digest mismatch"):
        validate_public_body(bad, current_listing_urls())


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
    payload = build_payload(listings, [], CANONICAL_DASHBOARD_URL)

    assert payload["to"] == ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"]
    assert payload["cc"] == []
    assert payload["bcc"] == []
    assert CANONICAL_DASHBOARD_URL in payload["body_text"]

    with pytest.raises(ValueError):
        build_payload(listings, [], "https://example.com/keg")


def test_email_automation_mirror_uses_browser_gmail_route():
    text = Path("automation/kegerator-tracker-email.toml").read_text(encoding="utf-8")

    assert "lukestambaugh75@gmail.com" in text
    assert "devin.mullen89@gmail.com" in text
    assert "no CC/BCC" in text or "Do not add CC or BCC" in text
    assert "out/latest-email.json" in text
    assert "signed-in Chrome/Gmail browser route" in text
    assert "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/" in text
