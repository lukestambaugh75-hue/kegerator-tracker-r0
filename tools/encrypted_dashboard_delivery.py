#!/usr/bin/env python3
"""Prepare and finalize the scheduled encrypted Kegerator delivery."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT.parent / "Tools" / "encrypted-dashboard-publisher"
PUBLISHER_ROOT = ROOT.parent / "Encrypted Tracker Link Publisher r0"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from encrypted_dashboard_publisher.tracker_cli import run_tracker_cli  # noqa: E402
from tools.encrypted_dashboard_pilot import BINDING_ID, load_current_dashboard  # noqa: E402


TRACKER_ID = "kegerator"


def build_bundle() -> tuple[dict, dict]:
    dashboard = load_current_dashboard()
    email = json.loads((ROOT / "out" / "latest-email.json").read_text(encoding="utf-8"))
    brief = {
        "to": email.get("to"),
        "cc": email.get("cc") or [],
        "bcc": email.get("bcc") or [],
        "subject": email.get("subject") or "Kegerator tracker",
        "decision": dashboard["summary"]["decision"],
        "recommendation": dashboard["summary"]["recommendation"],
        "freshness": dashboard["source_freshness"],
    }
    return dashboard, brief


def main(argv=None) -> int:
    return run_tracker_cli(
        build_bundle,
        tracker_id=TRACKER_ID,
        binding_id=BINDING_ID,
        default_publisher_root=PUBLISHER_ROOT,
        default_tools_root=TOOLS_ROOT,
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
