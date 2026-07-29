from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


FIXTURE_NOW = datetime.now(timezone.utc).replace(microsecond=0)
START = FIXTURE_NOW - timedelta(minutes=4)
ATTEMPT = FIXTURE_NOW - timedelta(minutes=3)
AFTER = FIXTURE_NOW - timedelta(minutes=2)
PAYLOAD_AT = FIXTURE_NOW - timedelta(minutes=1)
PRE_SEND_AT = FIXTURE_NOW - timedelta(seconds=30)
FINISH = FIXTURE_NOW
ORIGIN_URL = "https://github.com/lukestambaugh75-hue/kegerator-tracker-r0.git"


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _status(success_at: str) -> dict:
    return {
        "data_refreshed_at_utc": success_at,
        "last_attempt_at_utc": success_at,
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


def _origin_for_head(root: Path, observed_at: datetime) -> dict:
    from scripts.refresh_state import utc_iso

    return {
        "status": "verified",
        "observed_at_utc": utc_iso(observed_at),
        "remote_name": "origin",
        "repository": "lukestambaugh75-hue/kegerator-tracker-r0",
        "fetch_url": ORIGIN_URL,
        "push_url": ORIGIN_URL,
        "live_main_sha": _run(root, "rev-parse", "HEAD"),
    }


@pytest.fixture()
def evidence_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    old_timestamp = _utc_text(START - timedelta(days=1))
    _write_json(root / "data/listings.json", [_listing(old_timestamp)])
    _write_json(root / "data/specs.json", [])
    _write_json(root / "data/refresh-status.json", _status(old_timestamp))
    (root / "assets").mkdir()
    (root / "assets/kegerator-hero.png").write_bytes(b"image")
    (root / "index.html").write_text("<html>Kegerator Tracker</html>\n", encoding="utf-8")
    (root / "history.csv").write_text(
        "date,brand,model,retailer,price,list_price,source,data_quality\n"
        f"{(START.date() - timedelta(days=1)).isoformat()},Kegco,K309B-1,Home Depot,800,900,https://example.com/item,confirmed\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        ".DS_Store\n.cache/\n__pycache__/\n.pytest_cache/\nout/\n*.pyc\n",
        encoding="utf-8",
    )
    (root / "Makefile").write_text("verify-current:\n\t@true\n", encoding="utf-8")
    (root / "scripts").mkdir()
    source_root = Path(__file__).resolve().parents[1]
    for relative in (
        "scripts/audience_guard.py",
        "scripts/refresh.py",
        "scripts/refresh_state.py",
        "scripts/repair_history.py",
        "scripts/run_evidence.py",
        "tests/test_run_evidence.py",
        "tests/test_tracker.py",
        "tools/build_email.py",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    (root / "scripts/check_public_pages.py").write_text(
        '''from __future__ import annotations
import json
from scripts.refresh_state import build_payload_source_identity

PUBLIC_URL = "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/"
DEPLOYED_PATHS = ("index.html",)

def verify_public_deployment(root, *, expected_sha):
    listings = json.loads((root / "data/listings.json").read_text())
    specs = json.loads((root / "data/specs.json").read_text())
    status = json.loads((root / "data/refresh-status.json").read_text())
    identity = build_payload_source_identity(listings, specs, status)
    origin = {
        "repository": "lukestambaugh75-hue/kegerator-tracker-r0",
        "fetch_url": "https://github.com/lukestambaugh75-hue/kegerator-tracker-r0.git",
        "push_url": "https://github.com/lukestambaugh75-hue/kegerator-tracker-r0.git",
        "live_main_sha": expected_sha,
    }
    proof = {
        "result_sha": expected_sha,
        "origin_main_sha": expected_sha,
        "source_sha": identity["source_sha256"],
        "bundle_marker_sha256": "b" * 64,
        "files": {"index.html": "a" * 64},
        "public_url": PUBLIC_URL,
        "public_state": "Fresh",
        "data_refreshed_at_utc": identity["data_refreshed_at_utc"],
        "fetches": {"index.html": {"url": PUBLIC_URL, "final_url": PUBLIC_URL, "status": 200, "sha256": "a" * 64}},
        "origin": origin,
    }
    return {"state": "Fresh", "data_refreshed_at_utc": identity["data_refreshed_at_utc"]}, proof
''',
        encoding="utf-8",
    )
    _run(root, "init", "-b", "main")
    _run(root, "config", "user.name", "Test")
    _run(root, "config", "user.email", "test@example.com")
    _run(root, "remote", "add", "origin", ORIGIN_URL)
    _run(
        root,
        "add",
        ".gitignore",
        "Makefile",
        "assets/kegerator-hero.png",
        "data/listings.json",
        "data/specs.json",
        "data/refresh-status.json",
        "history.csv",
        "index.html",
        "scripts/refresh.py",
        "scripts/refresh_state.py",
        "scripts/repair_history.py",
        "scripts/audience_guard.py",
        "scripts/check_public_pages.py",
        "scripts/run_evidence.py",
        "tests/test_run_evidence.py",
        "tests/test_tracker.py",
        "tools/build_email.py",
    )
    _run(root, "commit", "-m", "initial")
    return root


def _start(root: Path, *, run_id: str = "run-20260728", now: datetime = START) -> Path:
    from scripts.run_evidence import create_run_state

    state_path = root / "out/run-state.json"
    create_run_state(
        root,
        state_path,
        run_id,
        "kegerator-tracker-email",
        "scheduled-email",
        owner_pid=os.getpid(),
        now=now,
        origin_reader=_origin_for_head,
    )
    return state_path


def _refresh_runner(
    root: Path,
    *,
    forged_count: int | None = None,
    wrong_run: bool = False,
    redirect_outcome: Path | None = None,
):
    from scripts.refresh_state import build_refresh_target_identity
    from scripts.refresh import write_json_exclusive

    def runner(command, **kwargs):
        command = [str(value) for value in command]
        outcome_path = Path(command[command.index("--outcome-path") + 1])
        if redirect_outcome is not None:
            outcome_path = redirect_outcome
        run_id = command[command.index("--run-id") + 1]
        attempted = _utc_text(ATTEMPT)
        before = json.loads((root / "data/listings.json").read_text(encoding="utf-8"))
        target = build_refresh_target_identity(before)
        _write_json(root / "data/listings.json", [_listing(attempted, 750)])
        _write_json(root / "data/refresh-status.json", _status(attempted))
        count = forged_count if forged_count is not None else target["source_count"]
        outcome = {
            "status": "success",
            "reason": "1 of 1 targets confirmed from current evidence.",
            "attempted_at_utc": attempted,
            "expected_count": count,
            "confirmed_count": count,
            "failed_count": 0,
            "history_appended": 0,
            "run_id": "another-run" if wrong_run else run_id,
            "input_source_count": count,
            "target_manifest_sha256": target["target_manifest_sha256"],
        }
        inherited = tuple(kwargs.get("pass_fds") or ())
        if inherited:
            assert len(inherited) == 1
            assert kwargs["env"]["KEG_EVIDENCE_DIR_FD"] == str(inherited[0])
            write_json_exclusive(outcome_path, outcome, dir_fd=inherited[0])
        else:
            _write_json(outcome_path, outcome)
        return subprocess.CompletedProcess(command, 0, stdout=b"refresh success\n", stderr=b"")

    return runner


def _verification_runner(exit_code: int = 0):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            exit_code,
            stdout=b"verification output\n",
            stderr=b"" if exit_code == 0 else b"verification failed\n",
        )

    return runner


def _refresh_and_verify(root: Path, state_path: Path) -> dict:
    from scripts.run_evidence import execute_refresh_once, run_local_verification

    execute_refresh_once(root, state_path, now=ATTEMPT, runner=_refresh_runner(root))
    return run_local_verification(
        root,
        state_path,
        now=AFTER,
        runner=_verification_runner(),
    )


def _commit_and_bind(root: Path, state_path: Path) -> dict:
    from scripts.run_evidence import record_result_sha

    _run(root, "add", "data/listings.json", "data/refresh-status.json", "history.csv")
    if _run(root, "diff", "--cached", "--name-only"):
        _run(root, "commit", "-m", "refresh")
    return record_result_sha(
        root,
        state_path,
        now=AFTER,
        origin_reader=_origin_for_head,
    )


def _fake_deployment(root: Path, expected_sha: str):
    from scripts.check_public_pages import DEPLOYED_PATHS, PUBLIC_URL
    from scripts.refresh_state import build_payload_source_identity

    listings = json.loads((root / "data/listings.json").read_text(encoding="utf-8"))
    specs = json.loads((root / "data/specs.json").read_text(encoding="utf-8"))
    status = json.loads((root / "data/refresh-status.json").read_text(encoding="utf-8"))
    identity = build_payload_source_identity(listings, specs, status)
    files = {relative: "a" * 64 for relative in DEPLOYED_PATHS}
    fetches = {
        relative: {
            "url": f"{PUBLIC_URL}{relative}",
            "final_url": f"{PUBLIC_URL}{relative}",
            "status": 200,
            "sha256": "a" * 64,
        }
        for relative in DEPLOYED_PATHS
    }
    proof = {
        "result_sha": expected_sha,
        "origin_main_sha": expected_sha,
        "source_sha": identity["source_sha256"],
        "bundle_marker_sha256": "b" * 64,
        "files": files,
        "public_url": PUBLIC_URL,
        "public_state": "Fresh",
        "data_refreshed_at_utc": identity["data_refreshed_at_utc"],
        "fetches": fetches,
        "origin": {
            "repository": "lukestambaugh75-hue/kegerator-tracker-r0",
            "fetch_url": ORIGIN_URL,
            "push_url": ORIGIN_URL,
            "live_main_sha": expected_sha,
        },
    }
    return {"state": "Fresh", "data_refreshed_at_utc": identity["data_refreshed_at_utc"]}, proof


def _through_deployment(root: Path, state_path: Path) -> dict:
    from scripts.run_evidence import record_deployment_evidence

    _refresh_and_verify(root, state_path)
    state = _commit_and_bind(root, state_path)
    return record_deployment_evidence(
        root,
        state_path,
        now=AFTER,
        verifier=_fake_deployment,
        origin_reader=_origin_for_head,
    )


def _write_current_payload(root: Path, *, now: datetime = PAYLOAD_AT) -> Path:
    from tools.build_email import build_payload, write_payload

    listings = json.loads((root / "data/listings.json").read_text(encoding="utf-8"))
    specs = json.loads((root / "data/specs.json").read_text(encoding="utf-8"))
    status = json.loads((root / "data/refresh-status.json").read_text(encoding="utf-8"))
    payload = build_payload(listings, specs, status, now=now)
    path = root / "out/latest-email.json"
    write_payload(path, payload)
    return path


def _through_pre_send(root: Path, state_path: Path) -> Path:
    from scripts.run_evidence import record_payload_evidence, record_pre_send_validation

    _through_deployment(root, state_path)
    payload_path = _write_current_payload(root)
    record_payload_evidence(
        root,
        state_path,
        payload_path,
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    record_pre_send_validation(
        root,
        state_path,
        payload_path,
        now=PRE_SEND_AT,
        origin_reader=_origin_for_head,
    )
    return payload_path


def test_adapter_executes_one_refresh_and_binds_actual_target_inventory(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, execute_refresh_once

    state_path = _start(evidence_repo)
    state = execute_refresh_once(
        evidence_repo,
        state_path,
        now=ATTEMPT,
        runner=_refresh_runner(evidence_repo),
    )

    evidence = state["stages"]["freshness"]["evidence"]
    assert state["stages"]["freshness"]["status"] == "passed"
    assert evidence["command"][0] == "/usr/bin/python3"
    assert evidence["command"][1] == str(evidence_repo / "scripts/refresh.py")
    assert evidence["target_identity"]["source_count"] == 1
    assert evidence["outcome"]["expected_count"] == 1
    assert evidence["outcome"]["run_id"] == state["run_id"]
    assert evidence["exit_code"] == 0
    assert evidence["outcome_transport"] == "inherited_parent_directory_fd"
    with pytest.raises(RunEvidenceError, match="exactly once"):
        execute_refresh_once(
            evidence_repo,
            state_path,
            now=AFTER,
            runner=_refresh_runner(evidence_repo),
        )


def test_target_inventory_rejects_reused_source_url():
    from scripts.refresh_state import build_refresh_target_identity

    rows = [_listing(_utc_text(START)), _listing(_utc_text(START))]
    rows[1]["model"] = "DIFFERENT-MODEL"
    with pytest.raises(ValueError, match="reuses an earlier source URL"):
        build_refresh_target_identity(rows)


@pytest.mark.parametrize("variant", ["forged_count", "wrong_run"])
def test_refresh_rejects_forged_count_or_cross_run_outcome(evidence_repo: Path, variant: str):
    from scripts.run_evidence import RunEvidenceError, execute_refresh_once

    state_path = _start(evidence_repo)
    runner = _refresh_runner(
        evidence_repo,
        forged_count=2 if variant == "forged_count" else None,
        wrong_run=variant == "wrong_run",
    )
    with pytest.raises(RunEvidenceError, match="count|different run"):
        execute_refresh_once(evidence_repo, state_path, now=ATTEMPT, runner=runner)


def test_recorded_verification_is_required_before_result(evidence_repo: Path):
    from scripts.run_evidence import (
        RunEvidenceError,
        execute_refresh_once,
        record_result_sha,
        run_local_verification,
    )

    state_path = _start(evidence_repo)
    execute_refresh_once(
        evidence_repo,
        state_path,
        now=ATTEMPT,
        runner=_refresh_runner(evidence_repo),
    )
    _run(evidence_repo, "add", "data/listings.json", "data/refresh-status.json")
    _run(evidence_repo, "commit", "-m", "refresh")
    with pytest.raises(RunEvidenceError, match="verification"):
        record_result_sha(
            evidence_repo,
            state_path,
            now=AFTER,
            origin_reader=_origin_for_head,
        )
    state = run_local_verification(
        evidence_repo,
        state_path,
        now=AFTER,
        runner=_verification_runner(),
    )
    attempt = state["stages"]["verification"]["evidence"]["attempts"][0]
    assert attempt["command"] == ["/usr/bin/make", "verify-current"]
    assert attempt["cwd"] == str(evidence_repo)
    assert attempt["exit_code"] == 0
    assert attempt["stdout_sha256"]


def test_bounded_repair_records_command_path_exit_and_forces_verification_rerun(
    evidence_repo: Path,
):
    from scripts.run_evidence import (
        RunEvidenceError,
        execute_refresh_once,
        execute_repair,
        propose_repair,
        record_result_sha,
        run_local_verification,
    )

    (evidence_repo / "history.csv").write_text(
        "date,brand,model,retailer,price,list_price,source,data_quality\n"
        f"{(START.date() - timedelta(days=1)).isoformat()},Kegco,K309B-1,Home Depot,800,900,https://example.com/a,confirmed\n"
        "2026-07-26,Kegco,K309B-1,Home Depot,810,900,https://example.com/b,estimated\n",
        encoding="utf-8",
    )
    _run(evidence_repo, "add", "history.csv")
    _run(evidence_repo, "commit", "-m", "seed repair case")
    state_path = _start(evidence_repo)
    execute_refresh_once(
        evidence_repo,
        state_path,
        now=ATTEMPT,
        runner=_refresh_runner(evidence_repo),
    )
    with pytest.raises(RunEvidenceError, match="exited 7"):
        run_local_verification(
            evidence_repo,
            state_path,
            now=AFTER,
            runner=_verification_runner(7),
        )
    proposed = propose_repair(
        evidence_repo,
        state_path,
        "history-prune",
        now=AFTER,
    )
    proposal = proposed["stages"]["repair"]["evidence"]
    assert proposal["target_path"] == "history.csv"
    assert proposal["precheck_exit_code"] == 1
    assert proposal["remove_count"] == 1
    repaired = execute_repair(
        evidence_repo,
        state_path,
        "history-prune",
        now=AFTER,
    )
    proof = repaired["stages"]["repair"]["evidence"]
    assert proof["attempts_used"] == 1
    assert proof["repair_command"][-2:] == ["--path", str(evidence_repo / "history.csv")]
    assert proof["repair_exit_code"] == 0
    assert proof["postcheck_exit_code"] == 0
    with pytest.raises(RunEvidenceError, match="verification"):
        record_result_sha(
            evidence_repo,
            state_path,
            now=AFTER,
            origin_reader=_origin_for_head,
        )
    verified = run_local_verification(
        evidence_repo,
        state_path,
        now=PAYLOAD_AT,
        runner=_verification_runner(),
    )
    attempts = verified["stages"]["verification"]["evidence"]["attempts"]
    assert [attempt["exit_code"] for attempt in attempts] == [7, 0]
    assert attempts[1]["after_repair"] is True
    with pytest.raises(RunEvidenceError, match="matching allowlisted"):
        execute_repair(evidence_repo, state_path, "history-prune", now=PAYLOAD_AT)


def test_non_allowlisted_or_unproven_repair_is_rejected(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, propose_repair

    state_path = _start(evidence_repo)
    with pytest.raises(RunEvidenceError, match="allowlisted"):
        propose_repair(evidence_repo, state_path, "arbitrary-shell", now=AFTER)


@pytest.mark.parametrize(
    "mutation",
    ["extra_field", "changed_subject", "noncanonical_bytes", "stale_generated_at"],
)
def test_payload_requires_exact_schema_content_serialization_and_freshness(
    evidence_repo: Path,
    mutation: str,
):
    from scripts.run_evidence import RunEvidenceError, record_payload_evidence

    state_path = _start(evidence_repo)
    _through_deployment(evidence_repo, state_path)
    payload_path = _write_current_payload(evidence_repo)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    if mutation == "extra_field":
        payload["attachments"] = []
        _write_json(payload_path, payload)
    elif mutation == "changed_subject":
        payload["subject"] = "Changed but plausible"
        _write_json(payload_path, payload)
    elif mutation == "noncanonical_bytes":
        payload_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    else:
        payload["generated_at"] = _utc_text(START - timedelta(days=1))
        _write_json(payload_path, payload)
    with pytest.raises(RunEvidenceError, match="payload|predates|old"):
        record_payload_evidence(
            evidence_repo,
            state_path,
            payload_path,
            now=PAYLOAD_AT,
            origin_reader=_origin_for_head,
        )


def test_exact_current_payload_is_bound_to_run_source_and_recipients(evidence_repo: Path):
    from scripts.run_evidence import record_payload_evidence

    state_path = _start(evidence_repo)
    state = _through_deployment(evidence_repo, state_path)
    payload_path = _write_current_payload(evidence_repo)
    state = record_payload_evidence(
        evidence_repo,
        state_path,
        payload_path,
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    evidence = state["stages"]["payload"]["evidence"]
    assert evidence["to"] == ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"]
    assert evidence["cc"] == []
    assert evidence["bcc"] == []
    assert evidence["source_identity"] == state["stages"]["freshness"]["evidence"]["output_source_identity"]


def test_detached_receipt_is_never_trusted_and_delivered_is_unrepresentable(evidence_repo: Path):
    from scripts.run_evidence import (
        RunEvidenceError,
        finish_run,
        record_payload_evidence,
        record_pre_send_validation,
        record_receipt_evidence,
    )

    state_path = _start(evidence_repo)
    _through_deployment(evidence_repo, state_path)
    payload_path = _write_current_payload(evidence_repo)
    record_payload_evidence(
        evidence_repo,
        state_path,
        payload_path,
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    record_pre_send_validation(
        evidence_repo,
        state_path,
        payload_path,
        now=PRE_SEND_AT,
        origin_reader=_origin_for_head,
    )
    forged_receipt = evidence_repo / "out/receipt.json"
    _write_json(forged_receipt, {"receipt_id": "self-authored", "status": "sent"})
    with pytest.raises(RunEvidenceError, match="no trusted external receipt"):
        record_receipt_evidence(evidence_repo, state_path, forged_receipt, now=FINISH)
    with pytest.raises(RunEvidenceError, match="delivered is unsupported"):
        finish_run(
            evidence_repo,
            state_path,
            "delivered",
            now=FINISH,
            origin_reader=_origin_for_head,
        )
    state = finish_run(
        evidence_repo,
        state_path,
        "delivery_unverified",
        now=FINISH,
        origin_reader=_origin_for_head,
    )
    assert state["status"] == "delivery_unverified"
    assert state["stages"]["receipt"]["status"] == "unverified"
    assert "delivered" not in json.dumps(state)


def test_deployment_recorder_calls_verifier_itself_and_records_fetch_proof(evidence_repo: Path):
    from scripts.run_evidence import record_deployment_evidence

    state_path = _start(evidence_repo)
    _refresh_and_verify(evidence_repo, state_path)
    state = _commit_and_bind(evidence_repo, state_path)
    calls: list[str] = []

    def verifier(root: Path, *, expected_sha: str):
        calls.append(expected_sha)
        return _fake_deployment(root, expected_sha)

    state = record_deployment_evidence(
        evidence_repo,
        state_path,
        now=AFTER,
        verifier=verifier,
        origin_reader=_origin_for_head,
    )
    proof = state["stages"]["deployment"]["evidence"]
    assert calls == [state["result_sha"]]
    assert proof["origin_main_sha"] == state["result_sha"]
    assert set(proof["fetches"]) == set(proof["files"])
    with pytest.raises(TypeError):
        record_deployment_evidence(
            evidence_repo,
            state_path,
            proof=copy.deepcopy(proof),
        )


def test_public_verifier_compares_live_bytes_to_result_git_blobs_not_worktree(
    evidence_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import scripts.check_public_pages as pages

    head = _run(evidence_repo, "rev-parse", "HEAD")
    committed = {
        relative: subprocess.run(
            ["git", "-C", str(evidence_repo), "show", f"{head}:{relative}"],
            capture_output=True,
            check=True,
        ).stdout
        for relative in pages.DEPLOYED_PATHS
    }
    monkeypatch.setattr(
        pages,
        "_live_origin",
        lambda root: {
            "repository": "lukestambaugh75-hue/kegerator-tracker-r0",
            "fetch_url": ORIGIN_URL,
            "push_url": ORIGIN_URL,
            "live_main_sha": head,
        },
    )
    monkeypatch.setattr(pages, "validate_public_body", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pages,
        "validate_public_status",
        lambda status, listings: {
            "state": "Fresh",
            "data_refreshed_at_utc": status["data_refreshed_at_utc"],
        },
    )
    dirty = committed["index.html"] + b"dirty-worktree-substitution"
    (evidence_repo / "index.html").write_bytes(dirty)

    def forged_fetch(url: str):
        relative = url.removeprefix(pages.PUBLIC_URL)
        raw = dirty if relative == "index.html" else committed[relative]
        return 200, raw, url

    with pytest.raises(Exception, match="differs from local HEAD bytes"):
        pages.verify_public_deployment(
            evidence_repo,
            expected_sha=head,
            fetcher=forged_fetch,
        )

    def exact_fetch(url: str):
        relative = url.removeprefix(pages.PUBLIC_URL)
        return 200, committed[relative], url

    _, proof = pages.verify_public_deployment(
        evidence_repo,
        expected_sha=head,
        fetcher=exact_fetch,
    )
    assert proof["files"]["index.html"] != __import__("hashlib").sha256(dirty).hexdigest()
    assert proof["fetches"]["index.html"]["final_url"] == pages.PUBLIC_URL + "index.html"


def test_public_verifier_rejects_redirected_fetch(evidence_repo: Path, monkeypatch):
    import scripts.check_public_pages as pages

    head = _run(evidence_repo, "rev-parse", "HEAD")
    monkeypatch.setattr(
        pages,
        "_live_origin",
        lambda root: {
            "repository": "lukestambaugh75-hue/kegerator-tracker-r0",
            "fetch_url": ORIGIN_URL,
            "push_url": ORIGIN_URL,
            "live_main_sha": head,
        },
    )

    def redirect(url: str):
        return 200, b"anything", "https://example.com/redirected"

    with pytest.raises(Exception, match="redirected"):
        pages.verify_public_deployment(evidence_repo, expected_sha=head, fetcher=redirect)


def test_state_path_must_be_canonical_ignored_and_not_a_symlink(evidence_repo: Path, tmp_path: Path):
    from scripts.run_evidence import RunEvidenceError, create_run_state

    with pytest.raises(RunEvidenceError, match="exactly out/run-state.json"):
        create_run_state(
            evidence_repo,
            evidence_repo / "out/alternate.json",
            "run-a",
            "kegerator-tracker-email",
            "scheduled-email",
            owner_pid=os.getpid(),
            now=START,
            origin_reader=_origin_for_head,
        )
    outside = tmp_path / "outside.json"
    outside.write_text("preserve\n", encoding="utf-8")
    (evidence_repo / "out").mkdir(exist_ok=True)
    (evidence_repo / "out/run-state.json").symlink_to(outside)
    with pytest.raises(RunEvidenceError, match="symlink"):
        create_run_state(
            evidence_repo,
            evidence_repo / "out/run-state.json",
            "run-b",
            "kegerator-tracker-email",
            "scheduled-email",
            owner_pid=os.getpid(),
            now=START,
            origin_reader=_origin_for_head,
        )
    assert outside.read_text(encoding="utf-8") == "preserve\n"


def test_symlinked_out_directory_cannot_escape_repository(evidence_repo: Path, tmp_path: Path):
    from scripts.run_evidence import RunEvidenceError, create_run_state

    outside = tmp_path / "outside"
    outside.mkdir()
    (evidence_repo / "out").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RunEvidenceError, match="symlink"):
        create_run_state(
            evidence_repo,
            evidence_repo / "out/run-state.json",
            "run-a",
            "kegerator-tracker-email",
            "scheduled-email",
            owner_pid=os.getpid(),
            now=START,
            origin_reader=_origin_for_head,
        )
    assert not (outside / "run-state.json").exists()


def test_payload_leaf_symlink_is_rejected_without_overwriting_target(
    evidence_repo: Path,
    tmp_path: Path,
):
    from tools.build_email import build_payload, write_payload

    (evidence_repo / "out").mkdir()
    outside = tmp_path / "outside-payload.json"
    outside.write_text("preserve\n", encoding="utf-8")
    payload_path = evidence_repo / "out/latest-email.json"
    payload_path.symlink_to(outside)
    listings = json.loads((evidence_repo / "data/listings.json").read_text(encoding="utf-8"))
    specs = []
    status = json.loads((evidence_repo / "data/refresh-status.json").read_text(encoding="utf-8"))
    payload = build_payload(listings, specs, status, now=START)
    with pytest.raises(ValueError, match="symlink"):
        write_payload(payload_path, payload)
    assert outside.read_text(encoding="utf-8") == "preserve\n"


def test_payload_parent_symlink_is_rejected_without_writing_outside(
    evidence_repo: Path,
    tmp_path: Path,
):
    from tools.build_email import build_payload, write_payload

    outside = tmp_path / "outside-output"
    outside.mkdir()
    (evidence_repo / "out").symlink_to(outside, target_is_directory=True)
    listings = json.loads((evidence_repo / "data/listings.json").read_text(encoding="utf-8"))
    status = json.loads((evidence_repo / "data/refresh-status.json").read_text(encoding="utf-8"))
    payload = build_payload(listings, [], status, now=START)
    with pytest.raises(ValueError, match="symlink"):
        write_payload(evidence_repo / "out/latest-email.json", payload)
    assert not (outside / "latest-email.json").exists()


def test_early_failure_finalizes_without_result_and_records_origin_failure(evidence_repo: Path):
    from scripts.run_evidence import finalize_failure

    state_path = _start(evidence_repo)

    def unavailable(root: Path, observed_at: datetime):
        raise RuntimeError("network unavailable")

    state = finalize_failure(
        evidence_repo,
        state_path,
        "audience",
        "audience_guard_failed",
        detail="fixed pre-send audience check failed",
        now=AFTER,
        origin_reader=unavailable,
    )
    assert state["status"] == "failed"
    assert state["result_sha"] is None
    assert state["finished_at_utc"] == _utc_text(AFTER)
    assert state["stages"]["blocker"]["status"] == "recorded"
    assert state["origin_at_finish"] == {
        "status": "unverified",
        "observed_at_utc": _utc_text(AFTER),
        "reason_code": "live_origin_check_failed",
    }


def test_cli_owned_refresh_failure_auto_finalizes_state(
    evidence_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import scripts.run_evidence as evidence

    state_path = _start(evidence_repo)
    forged = _refresh_runner(evidence_repo, forged_count=2)
    monkeypatch.setattr(
        evidence,
        "_execute",
        lambda command, root, runner=subprocess.run, **kwargs: evidence._command_result(
            forged(command)
        ),
    )
    original_finalize = evidence.finalize_failure

    def safe_finalize(root, state_path, failure_stage, reason_code, **kwargs):
        return original_finalize(
            root,
            state_path,
            failure_stage,
            reason_code,
            detail=kwargs.get("detail"),
            origin_reader=_origin_for_head,
        )

    monkeypatch.setattr(evidence, "finalize_failure", safe_finalize)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_evidence.py",
            "--root",
            str(evidence_repo),
            "--state",
            str(state_path),
            "refresh",
        ],
    )
    assert evidence.main() == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["finished_at_utc"] is not None
    assert state["stages"]["blocker"]["evidence"]["failure_stage"] == "refresh"


def test_second_failed_verification_auto_finalizes_after_repair_budget(
    evidence_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import scripts.run_evidence as evidence

    (evidence_repo / "history.csv").write_text(
        "date,brand,model,retailer,price,list_price,source,data_quality\n"
        f"{(START.date() - timedelta(days=1)).isoformat()},Kegco,K309B-1,Home Depot,800,900,https://example.com/a,confirmed\n"
        "2026-07-26,Kegco,K309B-1,Home Depot,810,900,https://example.com/b,estimated\n",
        encoding="utf-8",
    )
    _run(evidence_repo, "add", "history.csv")
    _run(evidence_repo, "commit", "-m", "seed second verification case")
    state_path = _start(evidence_repo)
    evidence.execute_refresh_once(
        evidence_repo,
        state_path,
        now=ATTEMPT,
        runner=_refresh_runner(evidence_repo),
    )
    with pytest.raises(evidence.RunEvidenceError):
        evidence.run_local_verification(
            evidence_repo,
            state_path,
            now=AFTER,
            runner=_verification_runner(7),
        )
    evidence._auto_finalize_cli_failure(
        evidence_repo,
        state_path,
        "verify",
        evidence.RunEvidenceError("first failure"),
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "running"
    evidence.propose_repair(evidence_repo, state_path, "history-prune", now=AFTER)
    evidence.execute_repair(evidence_repo, state_path, "history-prune", now=AFTER)
    with pytest.raises(evidence.RunEvidenceError):
        evidence.run_local_verification(
            evidence_repo,
            state_path,
            now=PAYLOAD_AT,
            runner=_verification_runner(9),
        )
    original_finalize = evidence.finalize_failure
    monkeypatch.setattr(
        evidence,
        "finalize_failure",
        lambda root, state, stage, reason, **kwargs: original_finalize(
            root,
            state,
            stage,
            reason,
            detail=kwargs.get("detail"),
            origin_reader=_origin_for_head,
        ),
    )
    evidence._auto_finalize_cli_failure(
        evidence_repo,
        state_path,
        "verify",
        evidence.RunEvidenceError("second failure"),
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert len(state["stages"]["verification"]["evidence"]["attempts"]) == 2


def test_stale_recovery_requires_exact_run_dead_owner_age_and_origin(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, recover_stale_run

    state_path = _start(evidence_repo)
    with pytest.raises(RunEvidenceError, match="run_id"):
        recover_stale_run(
            evidence_repo,
            state_path,
            "wrong-run",
            now=START + timedelta(hours=13),
            origin_reader=_origin_for_head,
            owner_probe=lambda pid: None,
        )
    with pytest.raises(RunEvidenceError, match="too recent"):
        recover_stale_run(
            evidence_repo,
            state_path,
            "run-20260728",
            now=START + timedelta(hours=1),
            origin_reader=_origin_for_head,
            owner_probe=lambda pid: None,
        )
    owner_token = json.loads(state_path.read_text(encoding="utf-8"))["owner"]["process_start_token"]
    with pytest.raises(RunEvidenceError, match="still live"):
        recover_stale_run(
            evidence_repo,
            state_path,
            "run-20260728",
            now=START + timedelta(hours=13),
            origin_reader=_origin_for_head,
            owner_probe=lambda pid: owner_token,
        )
    state = recover_stale_run(
        evidence_repo,
        state_path,
        "run-20260728",
        now=START + timedelta(hours=13),
        origin_reader=_origin_for_head,
        owner_probe=lambda pid: None,
    )
    assert state["status"] == "failed"
    assert state["recovery"]["status"] == "stale_owner_recovered"
    assert state["recovery"]["minimum_age_seconds"] == 12 * 60 * 60


def test_terminal_state_is_archived_before_next_run(evidence_repo: Path):
    from scripts.run_evidence import create_run_state, finalize_failure

    state_path = _start(evidence_repo, run_id="first-run")
    finalize_failure(
        evidence_repo,
        state_path,
        "test",
        "completed_test_failure",
        now=AFTER,
        origin_reader=_origin_for_head,
    )
    state = create_run_state(
        evidence_repo,
        state_path,
        "second-run",
        "kegerator-tracker-email",
        "scheduled-email",
        owner_pid=os.getpid(),
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    archives = list((evidence_repo / "out/run-state-archive").glob("first-run-*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["run_id"] == "first-run"
    assert state["run_id"] == "second-run"


def test_prior_terminal_schema_is_archived_but_prior_running_lane_is_preserved(
    evidence_repo: Path,
):
    from scripts.run_evidence import RunEvidenceError, create_run_state, finalize_failure

    state_path = _start(evidence_repo, run_id="legacy-terminal")
    finalize_failure(
        evidence_repo,
        state_path,
        "legacy",
        "legacy_terminal",
        now=AFTER,
        origin_reader=_origin_for_head,
    )
    legacy = json.loads(state_path.read_text(encoding="utf-8"))
    legacy["schema_version"] = 2
    legacy["stage_order"].remove("pre_send")
    legacy["stages"].pop("pre_send")
    _write_json(state_path, legacy)
    next_state = create_run_state(
        evidence_repo,
        state_path,
        "after-legacy",
        "kegerator-tracker-email",
        "scheduled-email",
        owner_pid=os.getpid(),
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    assert next_state["schema_version"] == 4
    archives = list((evidence_repo / "out/run-state-archive").glob("legacy-terminal-*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["schema_version"] == 2

    running_legacy = json.loads(state_path.read_text(encoding="utf-8"))
    running_legacy["schema_version"] = 2
    running_legacy["stage_order"].remove("pre_send")
    running_legacy["stages"].pop("pre_send")
    _write_json(state_path, running_legacy)
    with pytest.raises(RunEvidenceError, match="invalid or unfinished"):
        create_run_state(
            evidence_repo,
            state_path,
            "must-not-replace-running",
            "kegerator-tracker-email",
            "scheduled-email",
            owner_pid=os.getpid(),
            now=FINISH,
            origin_reader=_origin_for_head,
        )
    assert json.loads(state_path.read_text(encoding="utf-8"))["run_id"] == "after-legacy"


def test_origin_identity_is_required_at_start_result_and_finish(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, create_run_state, finish_run

    def wrong_origin(root: Path, observed_at: datetime):
        value = _origin_for_head(root, observed_at)
        value["fetch_url"] = "https://github.com/example/wrong.git"
        return value

    with pytest.raises(RunEvidenceError, match="unapproved URL"):
        create_run_state(
            evidence_repo,
            evidence_repo / "out/run-state.json",
            "run-a",
            "kegerator-tracker-email",
            "scheduled-email",
            owner_pid=os.getpid(),
            now=START,
            origin_reader=wrong_origin,
        )

    state_path = _start(evidence_repo)
    _through_deployment(evidence_repo, state_path)
    payload_path = _write_current_payload(evidence_repo)
    from scripts.run_evidence import record_payload_evidence, record_pre_send_validation

    record_payload_evidence(
        evidence_repo,
        state_path,
        payload_path,
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    record_pre_send_validation(
        evidence_repo,
        state_path,
        payload_path,
        now=PRE_SEND_AT,
        origin_reader=_origin_for_head,
    )

    def drifted_origin(root: Path, observed_at: datetime):
        value = _origin_for_head(root, observed_at)
        value["push_url"] = "git@github.com:example/wrong.git"
        return value

    with pytest.raises(RunEvidenceError, match="unapproved URL|changed"):
        finish_run(
            evidence_repo,
            state_path,
            "delivery_unverified",
            now=FINISH,
            origin_reader=drifted_origin,
        )


def test_pre_send_is_mandatory_and_finish_rejects_post_bind_payload_drift(
    evidence_repo: Path,
):
    from scripts.run_evidence import (
        RunEvidenceError,
        finish_run,
        record_payload_evidence,
        record_pre_send_validation,
    )

    state_path = _start(evidence_repo)
    _through_deployment(evidence_repo, state_path)
    payload_path = _write_current_payload(evidence_repo)
    record_payload_evidence(
        evidence_repo,
        state_path,
        payload_path,
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    with pytest.raises(RunEvidenceError, match="pre_send"):
        finish_run(
            evidence_repo,
            state_path,
            "delivery_unverified",
            now=FINISH,
            origin_reader=_origin_for_head,
        )
    record_pre_send_validation(
        evidence_repo,
        state_path,
        payload_path,
        now=PRE_SEND_AT,
        origin_reader=_origin_for_head,
    )
    original = payload_path.read_bytes()
    payload_path.write_bytes(original + b" ")
    with pytest.raises(RunEvidenceError, match="payload|serialization|drift"):
        finish_run(
            evidence_repo,
            state_path,
            "delivery_unverified",
            now=FINISH,
            origin_reader=_origin_for_head,
        )


def test_pre_send_rejects_old_binding_and_dirty_or_moved_result(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, record_payload_evidence, record_pre_send_validation

    state_path = _start(evidence_repo)
    _through_deployment(evidence_repo, state_path)
    payload_path = _write_current_payload(evidence_repo)
    record_payload_evidence(
        evidence_repo,
        state_path,
        payload_path,
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    with pytest.raises(RunEvidenceError, match="too old"):
        record_pre_send_validation(
            evidence_repo,
            state_path,
            payload_path,
            now=PAYLOAD_AT + timedelta(minutes=6),
            origin_reader=_origin_for_head,
        )
    (evidence_repo / "data/listings.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(RunEvidenceError, match="clean|drift|Git blob"):
        record_pre_send_validation(
            evidence_repo,
            state_path,
            payload_path,
            now=PRE_SEND_AT,
            origin_reader=_origin_for_head,
        )


def test_full_start_manifest_and_result_changed_path_allowlist_are_enforced(
    evidence_repo: Path,
):
    from scripts.run_evidence import RunEvidenceError, execute_refresh_once

    state_path = _start(evidence_repo)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    identity = state["stages"]["preflight"]["evidence"]["input_identity"]
    assert identity["tracked_manifest"]
    assert set(identity["fixed_command_blobs"]) >= {
        "scripts/run_evidence.py",
        "scripts/refresh.py",
        "tools/build_email.py",
    }
    (evidence_repo / "scripts/refresh.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    with pytest.raises(RunEvidenceError, match="drift|differs"):
        execute_refresh_once(
            evidence_repo,
            state_path,
            now=ATTEMPT,
            runner=_refresh_runner(evidence_repo),
        )

    _run(evidence_repo, "restore", "scripts/refresh.py")
    execute_refresh_once(
        evidence_repo,
        state_path,
        now=ATTEMPT,
        runner=_refresh_runner(evidence_repo),
    )
    from scripts.run_evidence import record_result_sha, run_local_verification

    run_local_verification(
        evidence_repo,
        state_path,
        now=AFTER,
        runner=_verification_runner(),
    )
    (evidence_repo / "index.html").write_text("<html>unexpected code change</html>\n", encoding="utf-8")
    _run(
        evidence_repo,
        "add",
        "data/listings.json",
        "data/refresh-status.json",
        "history.csv",
        "index.html",
    )
    _run(evidence_repo, "commit", "-m", "malicious mixed result")
    with pytest.raises(RunEvidenceError, match="allowlist|immutable"):
        record_result_sha(
            evidence_repo,
            state_path,
            now=AFTER,
            origin_reader=_origin_for_head,
        )


def test_result_provenance_rejects_commit_unrelated_to_run_start(evidence_repo: Path):
    import scripts.run_evidence as evidence

    state_path = _start(evidence_repo)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    tree = _run(evidence_repo, "rev-parse", "HEAD^{tree}")
    unrelated = subprocess.run(
        ["git", "-C", str(evidence_repo), "commit-tree", tree],
        input="unrelated\n",
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    state["result_sha"] = unrelated
    with pytest.raises(evidence.RunEvidenceError, match="not an ancestor"):
        evidence._assert_result_provenance(evidence_repo, state)


def test_flock_serializes_concurrent_refresh_and_preserves_exactly_once(
    evidence_repo: Path,
):
    from scripts.run_evidence import execute_refresh_once

    state_path = _start(evidence_repo)
    entered = threading.Event()
    release = threading.Event()
    second_runner_called = threading.Event()
    results: list[object] = []
    base_runner = _refresh_runner(evidence_repo)

    def slow_runner(command, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return base_runner(command, **kwargs)

    def forbidden_runner(command, **kwargs):
        second_runner_called.set()
        return base_runner(command, **kwargs)

    def invoke(runner):
        try:
            results.append(
                execute_refresh_once(
                    evidence_repo,
                    state_path,
                    now=ATTEMPT,
                    runner=runner,
                )
            )
        except Exception as exc:
            results.append(exc)

    first = threading.Thread(target=invoke, args=(slow_runner,))
    second = threading.Thread(target=invoke, args=(forbidden_runner,))
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.2)
    assert not second_runner_called.is_set()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert not second_runner_called.is_set()
    assert sum(isinstance(value, dict) for value in results) == 1
    assert any("exactly once" in str(value) for value in results if isinstance(value, Exception))


def test_repository_directory_flock_serializes_concurrent_starts(evidence_repo: Path):
    from scripts.run_evidence import create_run_state

    state_path = evidence_repo / "out/run-state.json"
    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []

    def slow_origin(root: Path, observed_at: datetime):
        entered.set()
        assert release.wait(timeout=5)
        return _origin_for_head(root, observed_at)

    def invoke(run_id: str, origin_reader):
        try:
            results.append(
                create_run_state(
                    evidence_repo,
                    state_path,
                    run_id,
                    "kegerator-tracker-email",
                    "scheduled-email",
                    owner_pid=os.getpid(),
                    now=START,
                    origin_reader=origin_reader,
                )
            )
        except Exception as exc:
            results.append(exc)

    first = threading.Thread(target=invoke, args=("concurrent-a", slow_origin))
    second = threading.Thread(target=invoke, args=("concurrent-b", _origin_for_head))
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.2)
    assert second.is_alive()
    assert results == []
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert sum(isinstance(value, dict) for value in results) == 1
    assert any("unfinished run state" in str(value) for value in results if isinstance(value, Exception))


def test_replacing_legacy_lock_path_cannot_split_lane_serialization(evidence_repo: Path):
    from scripts.run_evidence import execute_refresh_once

    state_path = _start(evidence_repo)
    entered = threading.Event()
    release = threading.Event()
    first_result: list[object] = []
    second_result: list[object] = []
    base_runner = _refresh_runner(evidence_repo)
    legacy_lock = evidence_repo / "out/run-evidence.lock"
    assert not legacy_lock.exists()
    legacy_lock.write_text("legacy replaceable pathname\n", encoding="utf-8")

    def slow_runner(command, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return base_runner(command, **kwargs)

    def invoke(target: list[object], runner):
        try:
            target.append(
                execute_refresh_once(
                    evidence_repo,
                    state_path,
                    now=ATTEMPT,
                    runner=runner,
                )
            )
        except Exception as exc:
            target.append(exc)

    first = threading.Thread(target=invoke, args=(first_result, slow_runner))
    first.start()
    assert entered.wait(timeout=5)
    legacy_lock.unlink()
    legacy_lock.write_text("replacement must not become the authority\n", encoding="utf-8")

    second = threading.Thread(
        target=invoke,
        args=(second_result, _refresh_runner(evidence_repo)),
    )
    second.start()
    time.sleep(0.2)
    assert second.is_alive(), "replacement lock path split the active transition lock"
    assert second_result == []
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert len(first_result) == 1 and isinstance(first_result[0], dict)
    assert len(second_result) == 1 and "exactly once" in str(second_result[0])


def test_replacing_nested_evidence_directory_is_detected_before_acceptance(
    evidence_repo: Path,
):
    from scripts.run_evidence import RunEvidenceError, execute_refresh_once

    state_path = _start(evidence_repo)
    entered = threading.Event()
    release = threading.Event()
    base_runner = _refresh_runner(evidence_repo)
    result: list[object] = []

    def slow_runner(command, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return base_runner(command, **kwargs)

    def invoke():
        try:
            result.append(
                execute_refresh_once(
                    evidence_repo,
                    state_path,
                    now=ATTEMPT,
                    runner=slow_runner,
                )
            )
        except Exception as exc:
            result.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert entered.wait(timeout=5)
    run_dir = evidence_repo / "out/runs/run-20260728"
    moved = evidence_repo / "out/runs/replaced-during-transition"
    run_dir.rename(moved)
    run_dir.mkdir()
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(result) == 1 and isinstance(result[0], RunEvidenceError)
    assert "identity changed" in str(result[0])
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["stages"]["freshness"]["status"] == "in_progress"


def test_replacing_out_directory_fails_the_held_transition(evidence_repo: Path):
    from scripts.run_evidence import RunEvidenceError, execute_refresh_once

    state_path = _start(evidence_repo)
    entered = threading.Event()
    release = threading.Event()
    base_runner = _refresh_runner(evidence_repo)
    result: list[object] = []

    def slow_runner(command, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return base_runner(command, **kwargs)

    def invoke():
        try:
            result.append(
                execute_refresh_once(
                    evidence_repo,
                    state_path,
                    now=ATTEMPT,
                    runner=slow_runner,
                )
            )
        except Exception as exc:
            result.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert entered.wait(timeout=5)
    out = evidence_repo / "out"
    moved = evidence_repo / "out-replaced-during-transition"
    out.rename(moved)
    out.mkdir()
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(result) == 1 and isinstance(result[0], RunEvidenceError)
    assert "out directory identity changed" in str(result[0])
    persisted = json.loads((moved / "run-state.json").read_text(encoding="utf-8"))
    assert persisted["stages"]["freshness"]["status"] == "in_progress"
    assert not (out / "run-state.json").exists()


def test_replacing_state_file_is_detected_before_atomic_transition_write(
    evidence_repo: Path,
):
    from scripts.run_evidence import RunEvidenceError, execute_refresh_once

    state_path = _start(evidence_repo)
    entered = threading.Event()
    release = threading.Event()
    base_runner = _refresh_runner(evidence_repo)
    result: list[object] = []

    def slow_runner(command, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return base_runner(command, **kwargs)

    def invoke():
        try:
            result.append(
                execute_refresh_once(
                    evidence_repo,
                    state_path,
                    now=ATTEMPT,
                    runner=slow_runner,
                )
            )
        except Exception as exc:
            result.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert entered.wait(timeout=5)
    replacement = evidence_repo / "out/replacement-state.json"
    replacement.write_bytes(state_path.read_bytes())
    os.replace(replacement, state_path)
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(result) == 1 and isinstance(result[0], RunEvidenceError)
    assert "evidence file identity changed" in str(result[0])
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["stages"]["freshness"]["status"] == "in_progress"


def test_evidence_rejects_hardlinks_ignores_legacy_lock_symlink_and_outcome_is_exclusive(
    evidence_repo: Path,
    tmp_path: Path,
):
    from scripts.refresh import write_json_exclusive
    from scripts.run_evidence import RunEvidenceError, create_run_state

    out = evidence_repo / "out"
    out.mkdir()
    outside_state = tmp_path / "outside-state.json"
    outside_state.write_text("preserve-state\n", encoding="utf-8")
    os.link(outside_state, out / "run-state.json")
    with pytest.raises(RunEvidenceError, match="uniquely owned regular"):
        create_run_state(
            evidence_repo,
            out / "run-state.json",
            "run-lock",
            "kegerator-tracker-email",
            "scheduled-email",
            owner_pid=os.getpid(),
            now=START,
            origin_reader=_origin_for_head,
        )
    assert outside_state.read_text(encoding="utf-8") == "preserve-state\n"
    (out / "run-state.json").unlink()

    outside_lock = tmp_path / "outside.lock"
    outside_lock.write_text("preserve-lock\n", encoding="utf-8")
    (out / "run-evidence.lock").symlink_to(outside_lock)
    state = create_run_state(
        evidence_repo,
        out / "run-state.json",
        "run-lock",
        "kegerator-tracker-email",
        "scheduled-email",
        owner_pid=os.getpid(),
        now=START,
        origin_reader=_origin_for_head,
    )
    assert state["status"] == "running"
    assert outside_lock.read_text(encoding="utf-8") == "preserve-lock\n"

    outcome = tmp_path / "outcome.json"
    write_json_exclusive(outcome, {"attempt": 1})
    original = outcome.read_bytes()
    with pytest.raises(FileExistsError):
        write_json_exclusive(outcome, {"attempt": 2})
    assert outcome.read_bytes() == original

    held = tmp_path / "held-outcome-parent"
    held.mkdir()
    routed_elsewhere = tmp_path / "replaceable-parent/outcome.json"
    held_fd = os.open(held, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        write_json_exclusive(routed_elsewhere, {"attempt": "fd-anchored"}, dir_fd=held_fd)
    finally:
        os.close(held_fd)
    assert json.loads((held / "outcome.json").read_text(encoding="utf-8")) == {
        "attempt": "fd-anchored"
    }
    assert not routed_elsewhere.exists()


def test_interrupted_verification_is_persisted_then_recovery_marks_review_required(
    evidence_repo: Path,
):
    from scripts.run_evidence import RunEvidenceError, execute_refresh_once, recover_stale_run, run_local_verification

    state_path = _start(evidence_repo)
    execute_refresh_once(
        evidence_repo,
        state_path,
        now=ATTEMPT,
        runner=_refresh_runner(evidence_repo),
    )

    def crash(command, **kwargs):
        raise RuntimeError("simulated process loss")

    with pytest.raises(RuntimeError, match="process loss"):
        run_local_verification(evidence_repo, state_path, now=AFTER, runner=crash)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["stages"]["verification"]["status"] == "in_progress"
    assert persisted["stages"]["verification"]["evidence"]["attempts"][0]["status"] == "in_progress"
    recovered = recover_stale_run(
        evidence_repo,
        state_path,
        "run-20260728",
        now=START + timedelta(hours=13),
        origin_reader=_origin_for_head,
        owner_probe=lambda pid: None,
    )
    assert recovered["status"] == "failed"
    assert recovered["stages"]["verification"]["status"] == "review_required"
    assert recovered["stages"]["blocker"]["status"] == "recorded"


def test_interrupted_repair_consumes_attempt_before_spawn_and_requires_review(
    evidence_repo: Path,
):
    from scripts.run_evidence import (
        execute_refresh_once,
        execute_repair,
        finalize_failure,
        propose_repair,
        run_local_verification,
    )

    (evidence_repo / "history.csv").write_text(
        "date,brand,model,retailer,price,list_price,source,data_quality\n"
        f"{(START.date() - timedelta(days=1)).isoformat()},Kegco,K309B-1,Home Depot,800,900,https://example.com/a,confirmed\n"
        "2026-07-26,Kegco,K309B-1,Home Depot,810,900,https://example.com/b,estimated\n",
        encoding="utf-8",
    )
    _run(evidence_repo, "add", "history.csv")
    _run(evidence_repo, "commit", "-m", "seed interrupted repair")
    state_path = _start(evidence_repo)
    execute_refresh_once(
        evidence_repo,
        state_path,
        now=ATTEMPT,
        runner=_refresh_runner(evidence_repo),
    )
    with pytest.raises(Exception):
        run_local_verification(
            evidence_repo,
            state_path,
            now=AFTER,
            runner=_verification_runner(7),
        )
    propose_repair(evidence_repo, state_path, "history-prune", now=AFTER)

    def crash(command, **kwargs):
        raise RuntimeError("repair process lost")

    with pytest.raises(RuntimeError, match="repair process lost"):
        execute_repair(
            evidence_repo,
            state_path,
            "history-prune",
            now=AFTER,
            runner=crash,
        )
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["stages"]["repair"]["status"] == "in_progress"
    assert persisted["stages"]["repair"]["evidence"]["attempts_used"] == 1
    terminal = finalize_failure(
        evidence_repo,
        state_path,
        "repair",
        "repair_process_lost",
        now=PAYLOAD_AT,
        origin_reader=_origin_for_head,
    )
    assert terminal["status"] == "failed"
    assert terminal["stages"]["repair"]["status"] == "review_required"
    assert terminal["stages"]["repair"]["evidence"]["attempts_used"] == 1


def test_direct_scheduled_deployment_and_payload_cli_establish_repo_root(
    evidence_repo: Path,
):
    state_path = _start(evidence_repo)
    _refresh_and_verify(evidence_repo, state_path)
    _commit_and_bind(evidence_repo, state_path)
    fake_bin = evidence_repo / "out/test-bin"
    fake_bin.mkdir(parents=True)
    head = _run(evidence_repo, "rev-parse", "HEAD")
    git_wrapper = fake_bin / "git"
    git_wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$3\" = \"ls-remote\" ]; then\n"
        f"  printf '%s\\trefs/heads/main\\n' '{head}'\n"
        "  exit 0\n"
        "fi\n"
        "exec /usr/bin/git \"$@\"\n",
        encoding="utf-8",
    )
    git_wrapper.chmod(0o700)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)
    deployment = subprocess.run(
        ["/usr/bin/python3", "scripts/run_evidence.py", "deployment"],
        cwd=evidence_repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert deployment.returncode == 0, deployment.stderr
    assert "deployment=passed" in deployment.stdout

    _write_current_payload(evidence_repo, now=datetime.now(timezone.utc))
    payload = subprocess.run(
        [
            "/usr/bin/python3",
            "scripts/run_evidence.py",
            "payload",
            "--payload",
            "out/latest-email.json",
        ],
        cwd=evidence_repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert payload.returncode == 0, payload.stderr
    assert "payload=passed" in payload.stdout
    pre_send = subprocess.run(
        [
            "/usr/bin/python3",
            "scripts/run_evidence.py",
            "pre-send",
            "--payload",
            "out/latest-email.json",
        ],
        cwd=evidence_repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert pre_send.returncode == 0, pre_send.stderr
    assert "pre_send=passed" in pre_send.stdout
    finish = subprocess.run(
        [
            "/usr/bin/python3",
            "scripts/run_evidence.py",
            "finish",
            "--outcome",
            "delivery_unverified",
        ],
        cwd=evidence_repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert finish.returncode == 0, finish.stderr
    assert "status=delivery_unverified" in finish.stdout
    assert "receipt=unverified" in finish.stdout
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "delivery_unverified"
    assert state["stages"]["payload"]["status"] == "passed"
    assert state["stages"]["payload"]["evidence"]["origin"]["live_main_sha"] == head


def test_state_schema_rejects_illegal_status_evidence_and_terminal_without_blocker(
    evidence_repo: Path,
):
    import scripts.run_evidence as evidence

    state_path = _start(evidence_repo)
    original = json.loads(state_path.read_text(encoding="utf-8"))
    illegal = copy.deepcopy(original)
    illegal["stages"]["verification"]["status"] = "maybe"
    with pytest.raises(evidence.RunEvidenceError, match="illegal status"):
        evidence._validate_state_shape(illegal)
    extra = copy.deepcopy(original)
    extra["stages"]["receipt"]["evidence"]["self_asserted"] = True
    with pytest.raises(evidence.RunEvidenceError, match="receipt evidence"):
        evidence._validate_state_shape(extra)
    no_blocker = copy.deepcopy(original)
    no_blocker["status"] = "failed"
    no_blocker["finished_at_utc"] = _utc_text(AFTER)
    no_blocker["origin_at_finish"] = _origin_for_head(evidence_repo, AFTER)
    with pytest.raises(evidence.RunEvidenceError, match="blocker"):
        evidence._validate_state_shape(no_blocker)


def test_terminal_summary_prints_all_required_lane_signals(evidence_repo: Path, capsys):
    import scripts.run_evidence as evidence

    state_path = _start(evidence_repo)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    evidence._print_summary(state)
    output = capsys.readouterr().out
    for field in (
        "freshness=pending",
        "verification=pending",
        "deployment=unverified",
        "payload=unverified",
        "blocker=clear",
        "receipt=unverified",
    ):
        assert field in output


def test_automation_contract_uses_owned_commands_and_never_claims_delivery():
    text = Path("automation/kegerator-tracker-email.toml").read_text(encoding="utf-8")

    assert "scripts/run_evidence.py refresh" in text
    assert "scripts/run_evidence.py verify" in text
    assert "scripts/run_evidence.py deployment" in text
    assert "scripts/run_evidence.py pre-send" in text
    assert "scripts/run_evidence.py finish --outcome delivery_unverified" in text
    assert "scripts/run_evidence.py recover-stale" in text
    assert "scripts/refresh.py --outcome-path" not in text
    assert "finish --outcome delivered" not in text
    assert "Receipt must remain `unverified`" in text
    assert "finish revalidates" in text.casefold()
    assert "documentation mirror only" in text.casefold()
    assert "do not copy this file over the live" in text.casefold()
