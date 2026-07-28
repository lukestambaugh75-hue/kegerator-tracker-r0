#!/usr/bin/env python3
"""Maintain detached, fail-closed evidence for one local automation run.

This adapter records proofs produced by the existing tracker workflow. It does
not refresh sources, commit, push, deploy, open a browser, or send email.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .audience_guard import (
        CANONICAL_DASHBOARD_URL,
        EXPECTED_RECIPIENTS,
        listing_source_urls,
        validate_email_payload,
    )
    from .refresh_state import build_payload_source_identity, parse_utc, utc_iso
except ImportError:
    from audience_guard import (
        CANONICAL_DASHBOARD_URL,
        EXPECTED_RECIPIENTS,
        listing_source_urls,
        validate_email_payload,
    )
    from refresh_state import build_payload_source_identity, parse_utc, utc_iso


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT / "out" / "run-state.json"
SCHEMA_VERSION = 1
CONTRACT_ROLE = "terminal_summary"
DEFAULT_WORKFLOW_ID = "kegerator-tracker-email"
STAGE_ORDER = [
    "preflight",
    "freshness",
    "blocker",
    "repair",
    "deployment",
    "payload",
    "receipt",
]
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
RECEIPT_TYPES = {"browser_send_confirmation", "mailbox_receipt"}
EXPECTED_DEPLOYED_PATHS = {
    "index.html",
    "assets/kegerator-hero.png",
    "data/listings.json",
    "data/specs.json",
    "data/refresh-status.json",
    "history.csv",
}


class RunEvidenceError(ValueError):
    """Raised when evidence cannot be tied to the active run."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _git_text(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.strip()


def _validate_id(value: str, label: str) -> str:
    value = str(value or "")
    if not ID_RE.fullmatch(value):
        raise RunEvidenceError(f"{label} must be a simple 1-128 character identifier")
    return value


def _resolve_state_path(root: Path, state_path: Path | str) -> Path:
    root = root.resolve()
    candidate = Path(state_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RunEvidenceError("run state must stay inside the repository out directory") from exc
    if not relative.parts or relative.parts[0] != "out":
        raise RunEvidenceError("run state must stay inside the repository out directory")
    if _git(root, "ls-files", "--error-unmatch", "--", os.fspath(relative), check=False).returncode == 0:
        raise RunEvidenceError("run state path must not be tracked by git")
    if _git(root, "check-ignore", "--quiet", "--", os.fspath(relative), check=False).returncode != 0:
        raise RunEvidenceError("run state path must be ignored by git")
    return candidate


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _stage(
    status: str,
    observed_at: datetime,
    run_id: str,
    source_sha: str | None,
    evidence: dict | None = None,
) -> dict:
    return {
        "status": status,
        "observed_at_utc": utc_iso(observed_at),
        "run_id": run_id,
        "source_sha": source_sha,
        "evidence": evidence or {},
    }


def _validate_state_shape(state: dict) -> None:
    required = {
        "schema_version",
        "contract_role",
        "run_id",
        "workflow_id",
        "lane_id",
        "repo_root",
        "started_at_utc",
        "finished_at_utc",
        "start_sha",
        "result_sha",
        "status",
        "stage_order",
        "stages",
    }
    if not isinstance(state, dict) or set(state) != required:
        raise RunEvidenceError("run state has an invalid top-level schema")
    if state["schema_version"] != SCHEMA_VERSION:
        raise RunEvidenceError("run state schema version is unsupported")
    if state["contract_role"] != CONTRACT_ROLE:
        raise RunEvidenceError("run state contract_role must be terminal_summary")
    _validate_id(state["run_id"], "run_id")
    _validate_id(state["workflow_id"], "workflow_id")
    _validate_id(state["lane_id"], "lane_id")
    if not COMMIT_RE.fullmatch(str(state["start_sha"] or "")):
        raise RunEvidenceError("run state start_sha is invalid")
    if state["result_sha"] is not None and not COMMIT_RE.fullmatch(str(state["result_sha"])):
        raise RunEvidenceError("run state result_sha is invalid")
    parse_utc(state["started_at_utc"])
    if state["finished_at_utc"] is not None:
        parse_utc(state["finished_at_utc"])
    if state["stage_order"] != STAGE_ORDER:
        raise RunEvidenceError("run state stage_order is invalid")
    if not isinstance(state["stages"], dict) or list(state["stages"]) != STAGE_ORDER:
        raise RunEvidenceError("run state stages are incomplete")
    for name, stage in state["stages"].items():
        if not isinstance(stage, dict) or set(stage) != {
            "status",
            "observed_at_utc",
            "run_id",
            "source_sha",
            "evidence",
        }:
            raise RunEvidenceError(f"run state stage {name} has an invalid schema")
        parse_utc(stage["observed_at_utc"])
        if stage["run_id"] != state["run_id"]:
            raise RunEvidenceError(f"run state stage {name} belongs to a different run")
        if stage["source_sha"] is not None and not re.fullmatch(r"[0-9a-f]{64}", stage["source_sha"]):
            raise RunEvidenceError(f"run state stage {name} source_sha is invalid")
        if not isinstance(stage["evidence"], dict):
            raise RunEvidenceError(f"run state stage {name} evidence must be an object")


def _load_state(root: Path, state_path: Path | str) -> tuple[Path, dict]:
    path = _resolve_state_path(root, state_path)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"run state is unavailable: {exc}") from exc
    _validate_state_shape(state)
    if Path(state["repo_root"]).resolve() != root.resolve():
        raise RunEvidenceError("run state belongs to a different repository root")
    return path, state


def _require_running(state: dict) -> None:
    if state["status"] != "running" or state["finished_at_utc"] is not None:
        raise RunEvidenceError("run state is already finished")


def _load_sources(root: Path) -> tuple[list[dict], list[dict], dict, dict]:
    try:
        listings = json.loads((root / "data" / "listings.json").read_text(encoding="utf-8"))
        specs = json.loads((root / "data" / "specs.json").read_text(encoding="utf-8"))
        refresh = json.loads((root / "data" / "refresh-status.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"current refresh inputs are unavailable: {exc}") from exc
    identity = build_payload_source_identity(listings, specs, refresh)
    return listings, specs, refresh, identity


def create_run_state(
    root: Path,
    state_path: Path | str,
    run_id: str,
    workflow_id: str,
    lane_id: str,
    *,
    now: datetime | None = None,
) -> dict:
    root = root.resolve()
    path = _resolve_state_path(root, state_path)
    run_id = _validate_id(run_id, "run_id")
    workflow_id = _validate_id(workflow_id, "workflow_id")
    lane_id = _validate_id(lane_id, "lane_id")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RunEvidenceError("existing run state is invalid; preserve it for review") from exc
        _validate_state_shape(previous)
        if previous["status"] == "running" or previous["finished_at_utc"] is None:
            raise RunEvidenceError("an unfinished run state already occupies this lane")
    if _git_text(root, "rev-parse", "--show-toplevel") != os.fspath(root):
        raise RunEvidenceError("run root must be the exact git repository root")
    branch = _git_text(root, "branch", "--show-current")
    start_sha = _git_text(root, "rev-parse", "HEAD")
    if branch != "main":
        raise RunEvidenceError("automation run must start on main")
    if _git_text(root, "rev-parse", "origin/main") != start_sha:
        raise RunEvidenceError("local main must match origin/main before the run starts")
    if _git_text(root, "status", "--porcelain", "--untracked-files=no"):
        raise RunEvidenceError("tracked worktree changes must be resolved before the run starts")
    start_identity = _load_sources(root)[3]
    start_source_sha = start_identity["source_sha256"]
    state = {
        "schema_version": SCHEMA_VERSION,
        "contract_role": CONTRACT_ROLE,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "lane_id": lane_id,
        "repo_root": os.fspath(root),
        "started_at_utc": utc_iso(observed_at),
        "finished_at_utc": None,
        "start_sha": start_sha,
        "result_sha": None,
        "status": "running",
        "stage_order": list(STAGE_ORDER),
        "stages": {
            "preflight": _stage(
                "passed",
                observed_at,
                run_id,
                start_source_sha,
                {"branch": branch, "start_sha": start_sha, "tracked_worktree_clean": True},
            ),
            "freshness": _stage("pending", observed_at, run_id, None),
            "blocker": _stage("clear", observed_at, run_id, None),
            "repair": _stage("not_required", observed_at, run_id, start_source_sha),
            "deployment": _stage("unverified", observed_at, run_id, None),
            "payload": _stage("unverified", observed_at, run_id, None),
            "receipt": _stage("unverified", observed_at, run_id, None),
        },
    }
    _validate_state_shape(state)
    _atomic_json(path, state)
    return state


def record_refresh_evidence(
    root: Path,
    state_path: Path | str,
    outcome_path: Path | str,
    *,
    now: datetime | None = None,
) -> dict:
    root = root.resolve()
    path, state = _load_state(root, state_path)
    _require_running(state)
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    try:
        outcome = json.loads(Path(outcome_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"refresh outcome evidence is unavailable: {exc}") from exc
    if not isinstance(outcome, dict):
        raise RunEvidenceError("refresh outcome evidence must be an object")
    outcome_status = str(outcome.get("status") or "").casefold()
    if outcome_status not in {"success", "blocked", "partial", "failed"}:
        raise RunEvidenceError("refresh outcome status is invalid")
    attempted_at = parse_utc(outcome.get("attempted_at_utc"))
    if attempted_at is None or attempted_at < parse_utc(state["started_at_utc"]):
        raise RunEvidenceError("refresh outcome does not belong to this run")
    if attempted_at > observed_at:
        raise RunEvidenceError("refresh outcome timestamp is in the future")
    listings, specs, refresh, identity = _load_sources(root)
    if refresh.get("last_attempt_at_utc") != utc_iso(attempted_at):
        raise RunEvidenceError("stored refresh attempt does not match the current run outcome")
    if refresh.get("last_attempt_status") != outcome_status:
        raise RunEvidenceError("stored refresh status does not match the current run outcome")
    expected_count = outcome.get("expected_count")
    confirmed = outcome.get("confirmed_count")
    failed = outcome.get("failed_count")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (expected_count, confirmed, failed)):
        raise RunEvidenceError("refresh outcome counts must be integers")
    if expected_count <= 0 or confirmed < 0 or failed < 0 or confirmed + failed != expected_count:
        raise RunEvidenceError("refresh outcome counts are incoherent")
    if outcome_status == "success":
        if confirmed != expected_count or failed != 0:
            raise RunEvidenceError("successful refresh outcome must confirm every target")
        if refresh.get("data_refreshed_at_utc") != utc_iso(attempted_at):
            raise RunEvidenceError("successful snapshot does not match the current run attempt")
        freshness_status = "passed"
        blocker_status = "clear"
        blocker_evidence: dict = {}
    else:
        reason = str(outcome.get("reason") or "").strip()
        if not reason:
            raise RunEvidenceError("non-successful refresh outcome requires a blocker reason")
        freshness_status = "blocked"
        blocker_status = "recorded"
        blocker_evidence = {"status": outcome_status, "reason": reason}
        if refresh.get("last_attempt_reason") != reason:
            raise RunEvidenceError("stored blocker reason does not match the current run outcome")
        if outcome_status in {"blocked", "failed"} and not (
            confirmed == 0 and failed == expected_count
        ):
            raise RunEvidenceError(f"{outcome_status} outcome counts are incoherent")
        if outcome_status == "partial" and not (
            0 < confirmed < expected_count and failed == expected_count - confirmed
        ):
            raise RunEvidenceError("partial outcome counts are incoherent")
    refresh_evidence = {
        "outcome_status": outcome_status,
        "attempted_at_utc": utc_iso(attempted_at),
        "expected_count": expected_count,
        "confirmed_count": confirmed,
        "failed_count": failed,
        "source_identity": identity,
        "listing_count": len(listings),
        "spec_count": len(specs),
    }
    source_sha = identity["source_sha256"]
    state["stages"]["freshness"] = _stage(
        freshness_status, observed_at, state["run_id"], source_sha, refresh_evidence
    )
    state["stages"]["blocker"] = _stage(
        blocker_status, observed_at, state["run_id"], source_sha, blocker_evidence
    )
    if state["stages"]["repair"]["status"] == "not_required":
        state["stages"]["repair"] = _stage(
            "not_required", observed_at, state["run_id"], source_sha
        )
    _atomic_json(path, state)
    return state


def propose_repair(
    root: Path,
    state_path: Path | str,
    repair_id: str,
    evidence_path: Path | str,
    *,
    now: datetime | None = None,
) -> dict:
    path, state = _load_state(root.resolve(), state_path)
    _require_running(state)
    repair_id = _validate_id(repair_id, "repair_id")
    if state["stages"]["freshness"]["status"] not in {"passed", "blocked"}:
        raise RunEvidenceError("repair proposal requires observed refresh evidence")
    if state["stages"]["repair"]["status"] != "not_required":
        raise RunEvidenceError("only one bounded repair may be proposed per run")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    evidence_file = Path(evidence_path)
    raw = evidence_file.read_bytes()
    if not raw:
        raise RunEvidenceError("repair evidence file must not be empty")
    source_sha = state["stages"]["freshness"]["source_sha"]
    state["stages"]["repair"] = _stage(
        "proposed",
        observed_at,
        state["run_id"],
        source_sha,
        {
            "repair_id": repair_id,
            "proposal_sha256": _sha256(raw),
            "proposal_file": evidence_file.name,
            "max_attempts": 1,
            "attempts_used": 0,
        },
    )
    _atomic_json(path, state)
    return state


def record_repair_evidence(
    root: Path,
    state_path: Path | str,
    repair_id: str,
    evidence_path: Path | str,
    *,
    now: datetime | None = None,
) -> dict:
    path, state = _load_state(root.resolve(), state_path)
    _require_running(state)
    repair = state["stages"]["repair"]
    if repair["status"] != "proposed" or repair["evidence"].get("repair_id") != repair_id:
        raise RunEvidenceError("repair attempt requires the matching proposed repair_id")
    if repair["evidence"].get("max_attempts") != 1 or repair["evidence"].get("attempts_used") != 0:
        raise RunEvidenceError("the bounded repair attempt has already been consumed")
    evidence_file = Path(evidence_path)
    raw = evidence_file.read_bytes()
    if not raw:
        raise RunEvidenceError("repair evidence file must not be empty")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    state["stages"]["repair"] = _stage(
        "passed",
        observed_at,
        state["run_id"],
        repair["source_sha"],
        {
            **repair["evidence"],
            "attempts_used": 1,
            "result_sha256": _sha256(raw),
            "result_file": evidence_file.name,
        },
    )
    _atomic_json(path, state)
    return state


def record_result_sha(
    root: Path,
    state_path: Path | str,
    *,
    now: datetime | None = None,
) -> dict:
    root = root.resolve()
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["freshness"]["status"] not in {"passed", "blocked"}:
        raise RunEvidenceError("refresh evidence must be recorded before result_sha")
    if _git_text(root, "branch", "--show-current") != "main":
        raise RunEvidenceError("result must remain on main")
    if _git_text(root, "status", "--porcelain", "--untracked-files=no"):
        raise RunEvidenceError("result_sha cannot bind an uncommitted tracked worktree")
    result_sha = _git_text(root, "rev-parse", "HEAD")
    if _git_text(root, "rev-parse", "origin/main") != result_sha:
        raise RunEvidenceError("result_sha must match the pushed origin/main ref")
    current_identity = _load_sources(root)[3]
    recorded_identity = state["stages"]["freshness"]["evidence"].get("source_identity")
    if current_identity != recorded_identity:
        raise RunEvidenceError("result inputs drifted after refresh evidence was recorded")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    state["result_sha"] = result_sha
    state["stages"]["preflight"]["evidence"]["result_bound_at_utc"] = utc_iso(observed_at)
    _atomic_json(path, state)
    return state


def record_deployment_proof(
    root: Path,
    state_path: Path | str,
    proof: dict,
    *,
    now: datetime | None = None,
) -> dict:
    root = root.resolve()
    path, state = _load_state(root, state_path)
    _require_running(state)
    if not state["result_sha"]:
        raise RunEvidenceError("result_sha must be bound before deployment proof")
    if not isinstance(proof, dict):
        raise RunEvidenceError("deployment proof must be an object")
    required = {
        "result_sha",
        "origin_main_sha",
        "source_sha",
        "bundle_marker_sha256",
        "files",
        "public_url",
    }
    if not required.issubset(proof):
        raise RunEvidenceError("deployment proof is incomplete")
    if proof["result_sha"] != state["result_sha"] or proof["origin_main_sha"] != state["result_sha"]:
        raise RunEvidenceError("deployment proof is not bound to this run result_sha")
    if not re.fullmatch(r"[0-9a-f]{64}", str(proof["bundle_marker_sha256"] or "")):
        raise RunEvidenceError("deployment proof marker is invalid")
    if not isinstance(proof["files"], dict) or set(proof["files"]) != EXPECTED_DEPLOYED_PATHS:
        raise RunEvidenceError("deployment proof must contain every exact deployed file digest")
    if any(not re.fullmatch(r"[0-9a-f]{64}", str(value or "")) for value in proof["files"].values()):
        raise RunEvidenceError("deployment proof contains an invalid file digest")
    if proof["public_url"] != CANONICAL_DASHBOARD_URL:
        raise RunEvidenceError("deployment proof public_url is not canonical")
    current_identity = _load_sources(root)[3]
    recorded_identity = state["stages"]["freshness"]["evidence"].get("source_identity")
    if current_identity != recorded_identity:
        raise RunEvidenceError("deployment proof cannot bind drifted refresh inputs")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    source_sha = current_identity["source_sha256"]
    if proof.get("source_sha") != source_sha:
        raise RunEvidenceError("deployment proof source_sha does not match the current refresh")
    marker_payload = json.dumps(
        {
            "result_sha": proof["result_sha"],
            "source_sha": proof["source_sha"],
            "files": proof["files"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if proof["bundle_marker_sha256"] != _sha256(marker_payload):
        raise RunEvidenceError("deployment proof marker does not bind the supplied file digests")
    state["stages"]["deployment"] = _stage(
        "passed", observed_at, state["run_id"], source_sha, proof
    )
    _atomic_json(path, state)
    return state


def record_payload_evidence(
    root: Path,
    state_path: Path | str,
    payload_path: Path | str,
    *,
    now: datetime | None = None,
) -> dict:
    root = root.resolve()
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["deployment"]["status"] != "passed":
        raise RunEvidenceError("deployment must be proven before payload evidence is accepted")
    payload_file = Path(payload_path)
    raw = payload_file.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"email payload evidence is invalid: {exc}") from exc
    listings, specs, refresh, current_identity = _load_sources(root)
    recorded_identity = state["stages"]["freshness"]["evidence"].get("source_identity")
    if current_identity != recorded_identity:
        raise RunEvidenceError("email payload inputs drifted after the current refresh")
    validate_email_payload(
        payload,
        listing_source_urls(listings),
        expected_source_identity=build_payload_source_identity(listings, specs, refresh),
    )
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    state["stages"]["payload"] = _stage(
        "passed",
        observed_at,
        state["run_id"],
        current_identity["source_sha256"],
        {
            "payload_sha256": _sha256(raw),
            "payload_file": payload_file.name,
            "to": payload["to"],
            "cc": payload["cc"],
            "bcc": payload["bcc"],
            "subject": payload.get("subject"),
            "source_identity": current_identity,
        },
    )
    _atomic_json(path, state)
    return state


def record_receipt_evidence(
    root: Path,
    state_path: Path | str,
    evidence_path: Path | str,
    *,
    now: datetime | None = None,
) -> dict:
    root = root.resolve()
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["payload"]["status"] != "passed":
        raise RunEvidenceError("payload evidence must pass before a receipt can be bound")
    evidence_file = Path(evidence_path)
    raw = evidence_file.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"outer receipt evidence is invalid: {exc}") from exc
    if not isinstance(evidence, dict) or evidence.get("evidence_type") not in RECEIPT_TYPES:
        raise RunEvidenceError("receipt requires outer browser or mailbox evidence")
    if (
        evidence.get("run_id") != state["run_id"]
        or evidence.get("workflow_id") != state["workflow_id"]
        or evidence.get("lane_id") != state["lane_id"]
    ):
        raise RunEvidenceError("receipt evidence belongs to a different run, workflow, or lane")
    payload = state["stages"]["payload"]["evidence"]
    for field in ("payload_sha256", "to", "cc", "bcc", "subject"):
        if evidence.get(field) != payload.get(field):
            raise RunEvidenceError(f"receipt evidence {field} does not match the validated payload")
    if evidence.get("to") != EXPECTED_RECIPIENTS or evidence.get("cc") != [] or evidence.get("bcc") != []:
        raise RunEvidenceError("receipt evidence violates the approved recipient boundary")
    receipt_id = str(evidence.get("receipt_id") or "").strip()
    if not receipt_id or len(receipt_id) > 512:
        raise RunEvidenceError("receipt evidence requires a bounded external receipt_id")
    observed_at = parse_utc(evidence.get("observed_at_utc"))
    if observed_at is None:
        raise RunEvidenceError("receipt evidence requires observed_at_utc")
    current_time = parse_utc(now or _now())
    assert current_time is not None
    if observed_at > current_time + timedelta(minutes=5):
        raise RunEvidenceError("receipt evidence timestamp is in the future")
    payload_at = parse_utc(state["stages"]["payload"]["observed_at_utc"])
    if observed_at < payload_at:
        raise RunEvidenceError("receipt evidence predates the validated payload")
    state["stages"]["receipt"] = _stage(
        "externally_verified",
        current_time,
        state["run_id"],
        state["stages"]["payload"]["source_sha"],
        {
            "evidence_type": evidence["evidence_type"],
            "evidence_sha256": _sha256(raw),
            "evidence_file": evidence_file.name,
            "observed_at_utc": utc_iso(observed_at),
            "receipt_id": receipt_id,
            "payload_sha256": payload["payload_sha256"],
        },
    )
    _atomic_json(path, state)
    return state


def finish_run(
    root: Path,
    state_path: Path | str,
    outcome: str,
    *,
    now: datetime | None = None,
) -> dict:
    path, state = _load_state(root.resolve(), state_path)
    _require_running(state)
    outcome = str(outcome or "").casefold()
    if outcome not in {"delivered", "blocked", "failed"}:
        raise RunEvidenceError("finish outcome must be delivered, blocked, or failed")
    if not state["result_sha"]:
        raise RunEvidenceError("run cannot finish without a bound result_sha")
    if outcome == "delivered":
        required = {"freshness": "passed", "deployment": "passed", "payload": "passed", "receipt": "externally_verified"}
        for stage_name, required_status in required.items():
            if state["stages"][stage_name]["status"] != required_status:
                raise RunEvidenceError(f"delivered run requires {stage_name}={required_status}")
    elif outcome == "blocked":
        if state["stages"]["blocker"]["status"] != "recorded":
            raise RunEvidenceError("blocked run requires recorded blocker evidence")
        if state["stages"]["receipt"]["status"] != "unverified":
            raise RunEvidenceError("blocked run cannot claim a delivery receipt")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    if observed_at < parse_utc(state["started_at_utc"]):
        raise RunEvidenceError("finish timestamp predates run start")
    state["finished_at_utc"] = utc_iso(observed_at)
    state["status"] = outcome
    _atomic_json(path, state)
    return state


def _print_summary(state: dict) -> None:
    print(
        f"run_id={state['run_id']} workflow_id={state['workflow_id']} "
        f"lane_id={state['lane_id']} status={state['status']} "
        f"start_sha={state['start_sha']} result_sha={state['result_sha'] or 'unbound'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--state", type=Path, default=Path("out/run-state.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--run-id", required=True)
    start.add_argument("--workflow-id", default=DEFAULT_WORKFLOW_ID)
    start.add_argument("--lane-id", required=True)
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--outcome", type=Path, required=True)
    propose = commands.add_parser("propose-repair")
    propose.add_argument("--repair-id", required=True)
    propose.add_argument("--evidence", type=Path, required=True)
    repair = commands.add_parser("repair")
    repair.add_argument("--repair-id", required=True)
    repair.add_argument("--evidence", type=Path, required=True)
    commands.add_parser("result")
    payload = commands.add_parser("payload")
    payload.add_argument("--payload", type=Path, required=True)
    receipt = commands.add_parser("receipt")
    receipt.add_argument("--evidence", type=Path, required=True)
    finish = commands.add_parser("finish")
    finish.add_argument("--outcome", choices=("delivered", "blocked", "failed"), required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "start":
            state = create_run_state(
                root,
                args.state,
                args.run_id,
                args.workflow_id,
                args.lane_id,
            )
        elif args.command == "refresh":
            state = record_refresh_evidence(root, args.state, args.outcome)
        elif args.command == "propose-repair":
            state = propose_repair(root, args.state, args.repair_id, args.evidence)
        elif args.command == "repair":
            state = record_repair_evidence(
                root, args.state, args.repair_id, args.evidence
            )
        elif args.command == "result":
            state = record_result_sha(root, args.state)
        elif args.command == "payload":
            state = record_payload_evidence(root, args.state, args.payload)
        elif args.command == "receipt":
            state = record_receipt_evidence(root, args.state, args.evidence)
        else:
            state = finish_run(root, args.state, args.outcome)
    except Exception as exc:
        print(f"run evidence rejected: {exc}", file=os.sys.stderr)
        return 1
    _print_summary(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
