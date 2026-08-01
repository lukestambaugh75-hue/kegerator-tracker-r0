import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _current():
    import csv

    from tools.encrypted_dashboard_pilot import build_dashboard

    listings = json.loads(Path("data/listings.json").read_text(encoding="utf-8"))
    specs = json.loads(Path("data/specs.json").read_text(encoding="utf-8"))
    refresh = json.loads(Path("data/refresh-status.json").read_text(encoding="utf-8"))
    with Path("history.csv").open(newline="", encoding="utf-8") as handle:
        history = list(csv.DictReader(handle))
    now = datetime.fromisoformat(refresh["data_refreshed_at_utc"].replace("Z", "+00:00"))
    return build_dashboard(listings, specs, refresh, history, now=now), listings


def test_encrypted_dashboard_has_all_current_rows_links_and_granular_details():
    dashboard, listings = _current()

    assert len(dashboard["rows"]) == len(listings) == 24
    assert {row["direct_url"] for row in dashboard["rows"]} == {
        row["source_url"] for row in listings
    }
    assert all(row["confidence"] == "Confirmed" for row in dashboard["rows"])
    assert all(row["history"] for row in dashboard["rows"])
    assert all("Garage suitability" in row["details"] for row in dashboard["rows"])
    assert all("Temperature range" in row["details"] for row in dashboard["rows"])
    assert dashboard["summary"]["decision"] == "Compare 24 current Kegerator offers"


def test_encrypted_dashboard_rejects_noncurrent_snapshot():
    dashboard, listings = _current()
    del dashboard
    import csv

    from tools.encrypted_dashboard_pilot import EncryptedPilotError, build_dashboard

    specs = json.loads(Path("data/specs.json").read_text(encoding="utf-8"))
    refresh = json.loads(Path("data/refresh-status.json").read_text(encoding="utf-8"))
    refresh = copy.deepcopy(refresh)
    refresh["last_attempt_status"] = "failed"
    refresh["last_attempt_reason"] = "synthetic failure"
    refresh["last_attempt_at_utc"] = "2026-08-03T00:00:00Z"
    with Path("history.csv").open(newline="", encoding="utf-8") as handle:
        history = list(csv.DictReader(handle))
    with pytest.raises(EncryptedPilotError):
        build_dashboard(
            listings,
            specs,
            refresh,
            history,
            now=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )


def test_publish_is_default_off(monkeypatch, tmp_path):
    from tools.encrypted_dashboard_pilot import EncryptedPilotError, PILOT_FLAG, publish_current

    monkeypatch.delenv(PILOT_FLAG, raising=False)
    with pytest.raises(EncryptedPilotError, match="publication is disabled"):
        publish_current(
            publisher_root=tmp_path / "publisher",
            tools_root=tmp_path / "tools",
            receipt_path=tmp_path / "receipt.json",
        )


def test_private_receipt_guard_accepts_exact_magic_link_and_rejects_tampering():
    from scripts.audience_guard import AudienceBoundaryError, validate_encrypted_pilot_receipt

    audience = "view_AAAAAAAAAAAAAAAAAAAAAAAA"
    binding = "binding_9YcJ2xQw8Vn4Lm7Rt5Kp3Hs6"
    snapshot = "snapshot_" + "a" * 64
    receipt = {
        "audience_id": audience,
        "binding_id": binding,
        "snapshot_id": snapshot,
        "ciphertext_sha256": "b" * 64,
        "dashboard_url": (
            "https://lukestambaugh75-hue.github.io/"
            f"encrypted-tracker-link-publisher-r0/e/{audience}/#k={'A' * 43}"
        ),
    }
    validate_encrypted_pilot_receipt(
        receipt, expected_snapshot_id=snapshot, expected_binding_id=binding
    )

    tampered = copy.deepcopy(receipt)
    tampered["dashboard_url"] = tampered["dashboard_url"].replace(
        "lukestambaugh75-hue.github.io", "example.com"
    )
    with pytest.raises(AudienceBoundaryError):
        validate_encrypted_pilot_receipt(
            tampered, expected_snapshot_id=snapshot, expected_binding_id=binding
        )
