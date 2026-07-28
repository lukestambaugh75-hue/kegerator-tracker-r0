from __future__ import annotations

import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest


START = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
ATTEMPT = datetime(2026, 7, 28, 12, 1, tzinfo=timezone.utc)
AFTER = datetime(2026, 7, 28, 12, 2, tzinfo=timezone.utc)
RECEIPT_AT = datetime(2026, 7, 28, 12, 3, tzinfo=timezone.utc)


def _run(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _listing(retrieved: str, price: float = 800) -> dict:
    return {
        "brand": "Kegco",
        "model": "K309B-1",
        "retailer": "Home Depot",
        "source_url": "https://www.homedepot.com/p/kegerator/K309B-1",
        "retrieved": retrieved,
        "data_quality": "confirmed",
        "current_price": price,
        "list_price": 900,
        "in_stock": True,
        "complete_kit": True,
        "tap_count": 1,
        "outdoor_rated": False,
        "garage_suitability": "Good - deep-chill + fan-forced",
    }


def _status(success_at: str, *, attempt_status: str = "success", reason=None) -> dict:
    return {
        "data_refreshed_at_utc": success_at,
        "last_attempt_at_utc": success_at,
        "last_attempt_status": attempt_status,
        "last_attempt_reason": reason,
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


@pytest.fixture()
def evidence_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    old_timestamp = "2026-07-27T12:00:00Z"
    _write_json(root / "data/listings.json", [_listing(old_timestamp)])
    _write_json(root / "data/specs.json", [])
    _write_json(root / "data/refresh-status.json", _status(old_timestamp))
    (root / "assets").mkdir()
    (root / "assets/kegerator-hero.png").write_bytes(b"image")
    (root / "index.html").write_text("<html>Kegerator Tracker</html>\n", encoding="utf-8")
    (root / "history.csv").write_text("date,brand\n", encoding="utf-8")
    (root / ".gitignore").write_text("out/\n", encoding="utf-8")
    _run(root, "init", "-b", "main")
    _run(root, "config", "user.name", "Test")
    _run(root, "config", "user.email", "test@example.com")
    _run(root, "add", ".gitignore", "assets/kegerator-hero.png", "data/listings.json", "data/specs.json", "data/refresh-status.json", "history.csv", "index.html")
    _run(root, "commit", "-m", "initial")
    _run(root, "update-ref", "refs/remotes/origin/main", _run(root, "rev-parse", "HEAD"))
    return root


def _start_successful_run(root: Path):
    from scripts.run_evidence import create_run_state, record_refresh_evidence

    state_path = root / "out/run-state.json"
    create_run_state(
        root,
        state_path,
        "run-20260728",
        "kegerator-tracker-email",
        "daily",
        now=START,
    )
    attempted = "2026-07-28T12:01:00Z"
    _write_json(root / "data/listings.json", [_listing(attempted, 750)])
    _write_json(root / "data/refresh-status.json", _status(attempted))
    outcome = {
        "status": "success",
        "reason": "1 of 1 targets confirmed from current evidence.",
        "attempted_at_utc": attempted,
        "expected_count": 1,
        "confirmed_count": 1,
        "failed_count": 0,
    }
    outcome_path = root / "out/refresh-outcome.json"
    _write_json(outcome_path, outcome)
    state = record_refresh_evidence(root, state_path, outcome_path, now=ATTEMPT)
    return state_path, state


def _commit_result(root: Path, state_path: Path):
    from scripts.run_evidence import record_result_sha

    _run(root, "add", "data/listings.json", "data/refresh-status.json")
    _run(root, "commit", "-m", "refresh")
    _run(root, "update-ref", "refs/remotes/origin/main", _run(root, "rev-parse", "HEAD"))
    return record_result_sha(root, state_path, now=AFTER)


def _deployment_proof(root: Path, state: dict) -> dict:
    from scripts.check_public_pages import DEPLOYED_PATHS, validate_deployment_bundle

    files = {relative: (root / relative).read_bytes() for relative in DEPLOYED_PATHS}
    return validate_deployment_bundle(
        files,
        dict(files),
        result_sha=state["result_sha"],
        origin_main_sha=state["result_sha"],
        source_sha=state["stages"]["freshness"]["source_sha"],
    )


def _record_deployment_and_payload(root: Path, state_path: Path, state: dict):
    from scripts.run_evidence import record_deployment_proof, record_payload_evidence
    from tools.build_email import build_payload

    proof = _deployment_proof(root, state)
    state = record_deployment_proof(root, state_path, proof, now=AFTER)
    listings = json.loads((root / "data/listings.json").read_text(encoding="utf-8"))
    specs = json.loads((root / "data/specs.json").read_text(encoding="utf-8"))
    status = json.loads((root / "data/refresh-status.json").read_text(encoding="utf-8"))
    payload = build_payload(listings, specs, status, now=AFTER)
    payload_path = root / "out/latest-email.json"
    _write_json(payload_path, payload)
    state = record_payload_evidence(root, state_path, payload_path, now=AFTER)
    return state, payload_path


def test_run_state_is_detached_terminal_summary_with_ordered_stage_identity(evidence_repo: Path):
    state_path, state = _start_successful_run(evidence_repo)

    assert state_path.is_file()
    assert _run(evidence_repo, "check-ignore", "out/run-state.json") == "out/run-state.json"
    assert state["contract_role"] == "terminal_summary"
    assert state["run_id"] == "run-20260728"
    assert state["workflow_id"] == "kegerator-tracker-email"
    assert state["lane_id"] == "daily"
    assert list(state["stages"]) == state["stage_order"]
    assert all(stage["run_id"] == state["run_id"] for stage in state["stages"].values())
    assert state["stages"]["freshness"]["status"] == "passed"
    assert state["stages"]["freshness"]["source_sha"]
    assert state["stages"]["receipt"]["status"] == "unverified"


def test_run_state_rejects_non_ignored_or_tracked_state_paths(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, create_run_state

    with pytest.raises(RunEvidenceError, match="out directory"):
        create_run_state(
            evidence_repo,
            evidence_repo / "run-state.json",
            "run-a",
            "workflow-a",
            "lane-a",
            now=START,
        )
    tracked = evidence_repo / "out/tracked.json"
    tracked.parent.mkdir()
    tracked.write_text("{}\n", encoding="utf-8")
    _run(evidence_repo, "add", "-f", "out/tracked.json")
    _run(evidence_repo, "commit", "-m", "track forbidden state")
    _run(evidence_repo, "update-ref", "refs/remotes/origin/main", _run(evidence_repo, "rev-parse", "HEAD"))
    with pytest.raises(RunEvidenceError, match="must not be tracked"):
        create_run_state(
            evidence_repo,
            tracked,
            "run-b",
            "workflow-b",
            "lane-b",
            now=START,
        )


def test_result_sha_rejects_uncommitted_or_unpushed_result(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, record_result_sha

    state_path, _ = _start_successful_run(evidence_repo)
    with pytest.raises(RunEvidenceError, match="uncommitted"):
        record_result_sha(evidence_repo, state_path, now=AFTER)
    _run(evidence_repo, "add", "data/listings.json", "data/refresh-status.json")
    _run(evidence_repo, "commit", "-m", "refresh")
    with pytest.raises(RunEvidenceError, match="pushed origin/main"):
        record_result_sha(evidence_repo, state_path, now=AFTER)


def test_deployment_bundle_binds_head_source_and_every_exact_byte(evidence_repo: Path):
    from scripts.audience_guard import AudienceBoundaryError
    from scripts.check_public_pages import DEPLOYED_PATHS, validate_deployment_bundle

    files = {relative: (evidence_repo / relative).read_bytes() for relative in DEPLOYED_PATHS}
    sha = "a" * 40
    source_sha = "b" * 64
    proof = validate_deployment_bundle(
        files,
        dict(files),
        result_sha=sha,
        origin_main_sha=sha,
        source_sha=source_sha,
    )
    assert proof["result_sha"] == sha
    assert proof["source_sha"] == source_sha
    assert set(proof["files"]) == set(DEPLOYED_PATHS)

    drifted = dict(files)
    drifted["data/refresh-status.json"] += b" "
    with pytest.raises(AudienceBoundaryError, match="differs from local HEAD bytes"):
        validate_deployment_bundle(
            files,
            drifted,
            result_sha=sha,
            origin_main_sha=sha,
            source_sha=source_sha,
        )
    with pytest.raises(AudienceBoundaryError, match="origin/main"):
        validate_deployment_bundle(
            files,
            dict(files),
            result_sha=sha,
            origin_main_sha="c" * 40,
            source_sha=source_sha,
        )


def test_deployment_proof_rejects_forged_marker_or_wrong_source(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, record_deployment_proof

    state_path, _ = _start_successful_run(evidence_repo)
    state = _commit_result(evidence_repo, state_path)
    proof = _deployment_proof(evidence_repo, state)
    forged = copy.deepcopy(proof)
    forged["bundle_marker_sha256"] = "0" * 64
    with pytest.raises(RunEvidenceError, match="marker"):
        record_deployment_proof(evidence_repo, state_path, forged, now=AFTER)
    wrong_source = copy.deepcopy(proof)
    wrong_source["source_sha"] = "f" * 64
    with pytest.raises(RunEvidenceError, match="source_sha"):
        record_deployment_proof(evidence_repo, state_path, wrong_source, now=AFTER)


def test_payload_source_identity_rejects_drift_from_current_refresh(evidence_repo: Path):
    from scripts.audience_guard import AudienceBoundaryError, validate_email_payload
    from scripts.refresh_state import build_payload_source_identity
    from tools.build_email import build_payload

    listings = json.loads((evidence_repo / "data/listings.json").read_text(encoding="utf-8"))
    specs = []
    status = json.loads((evidence_repo / "data/refresh-status.json").read_text(encoding="utf-8"))
    payload = build_payload(listings, specs, status, now=START)
    drifted = copy.deepcopy(listings)
    drifted[0]["current_price"] = 1
    expected = build_payload_source_identity(drifted, specs, status)

    with pytest.raises(AudienceBoundaryError, match="source identity"):
        validate_email_payload(
            payload,
            {listings[0]["source_url"]},
            expected_source_identity=expected,
        )


def test_receipt_stays_unverified_until_matching_outer_evidence_is_bound(evidence_repo: Path):
    from scripts.run_evidence import (
        RunEvidenceError,
        finish_run,
        record_receipt_evidence,
    )

    state_path, _ = _start_successful_run(evidence_repo)
    state = _commit_result(evidence_repo, state_path)
    state, _ = _record_deployment_and_payload(evidence_repo, state_path, state)
    assert state["stages"]["receipt"]["status"] == "unverified"
    with pytest.raises(RunEvidenceError, match="receipt=externally_verified"):
        finish_run(evidence_repo, state_path, "delivered", now=RECEIPT_AT)

    payload_evidence = state["stages"]["payload"]["evidence"]
    bad = {
        "evidence_type": "browser_send_confirmation",
        "run_id": state["run_id"],
        "workflow_id": state["workflow_id"],
        "lane_id": state["lane_id"],
        "payload_sha256": "0" * 64,
        "to": payload_evidence["to"],
        "cc": [],
        "bcc": [],
        "subject": payload_evidence["subject"],
        "observed_at_utc": "2026-07-28T12:03:00Z",
        "receipt_id": "browser-proof-1",
    }
    evidence_path = evidence_repo / "out/receipt.json"
    _write_json(evidence_path, bad)
    with pytest.raises(RunEvidenceError, match="payload_sha256"):
        record_receipt_evidence(evidence_repo, state_path, evidence_path, now=RECEIPT_AT)

    bad["payload_sha256"] = payload_evidence["payload_sha256"]
    _write_json(evidence_path, bad)
    state = record_receipt_evidence(evidence_repo, state_path, evidence_path, now=RECEIPT_AT)
    assert state["stages"]["receipt"]["status"] == "externally_verified"
    assert state["stages"]["receipt"]["evidence"]["payload_sha256"] == payload_evidence["payload_sha256"]
    state = finish_run(evidence_repo, state_path, "delivered", now=RECEIPT_AT)
    assert state["status"] == "delivered"
    assert state["finished_at_utc"] == "2026-07-28T12:03:00Z"


def test_only_one_matching_proposed_repair_attempt_can_be_recorded(evidence_repo: Path):
    from scripts.run_evidence import (
        RunEvidenceError,
        propose_repair,
        record_repair_evidence,
    )

    state_path, _ = _start_successful_run(evidence_repo)
    proposal = evidence_repo / "out/repair-proposal.txt"
    proposal.write_text("Run history repair check once.\n", encoding="utf-8")
    state = propose_repair(
        evidence_repo,
        state_path,
        "history-check",
        proposal,
        now=AFTER,
    )
    assert state["stages"]["repair"]["evidence"]["max_attempts"] == 1
    result = evidence_repo / "out/repair-result.txt"
    result.write_text("24 kept, 0 would remove\n", encoding="utf-8")
    with pytest.raises(RunEvidenceError, match="matching proposed"):
        record_repair_evidence(
            evidence_repo,
            state_path,
            "wrong-repair",
            result,
            now=AFTER,
        )
    state = record_repair_evidence(
        evidence_repo,
        state_path,
        "history-check",
        result,
        now=AFTER,
    )
    assert state["stages"]["repair"]["evidence"]["attempts_used"] == 1
    with pytest.raises(RunEvidenceError, match="matching proposed"):
        record_repair_evidence(
            evidence_repo,
            state_path,
            "history-check",
            result,
            now=AFTER,
        )


def test_blocked_refresh_records_blocker_and_cannot_claim_receipt(evidence_repo: Path):
    from scripts.run_evidence import create_run_state, record_refresh_evidence

    state_path = evidence_repo / "out/run-state.json"
    create_run_state(
        evidence_repo,
        state_path,
        "blocked-run",
        "kegerator-tracker-email",
        "daily",
        now=START,
    )
    status = _status("2026-07-27T12:00:00Z")
    status.update(
        last_attempt_at_utc="2026-07-28T12:01:00Z",
        last_attempt_status="blocked",
        last_attempt_reason="0 of 1 targets confirmed.",
    )
    _write_json(evidence_repo / "data/refresh-status.json", status)
    outcome = {
        "status": "blocked",
        "reason": "0 of 1 targets confirmed.",
        "attempted_at_utc": "2026-07-28T12:01:00Z",
        "expected_count": 1,
        "confirmed_count": 0,
        "failed_count": 1,
    }
    outcome_path = evidence_repo / "out/blocked.json"
    _write_json(outcome_path, outcome)
    state = record_refresh_evidence(evidence_repo, state_path, outcome_path, now=ATTEMPT)

    assert state["stages"]["freshness"]["status"] == "blocked"
    assert state["stages"]["blocker"]["status"] == "recorded"
    assert state["stages"]["receipt"]["status"] == "unverified"
