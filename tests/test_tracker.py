import csv
import io
import json
from pathlib import Path


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
    assert "Deal Trackers" in html


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
    payload = build_payload(listings, [], "https://example.com/keg")

    assert payload["to"] == ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"]
    assert payload["cc"] == []
    assert payload["bcc"] == []
    assert "https://example.com/keg" in payload["body_text"]


def test_email_automation_mirror_uses_browser_gmail_route():
    text = Path("automation/kegerator-tracker-email.toml").read_text(encoding="utf-8")

    assert "lukestambaugh75@gmail.com" in text
    assert "devin.mullen89@gmail.com" in text
    assert "no CC/BCC" in text or "Do not add CC or BCC" in text
    assert "out/latest-email.json" in text
    assert "signed-in Chrome/Gmail browser route" in text
    assert "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/" in text
