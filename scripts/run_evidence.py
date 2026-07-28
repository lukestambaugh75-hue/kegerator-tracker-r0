#!/usr/bin/env python3
"""Execute and record one fail-closed Kegerator automation run.

The adapter owns the commands whose evidence it records. It never commits,
pushes, deploys, opens a browser, or sends mail. Delivery remains unverified
until this repository has a trusted external receipt adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

try:
    from .audience_guard import (
        CANONICAL_DASHBOARD_URL,
        EXPECTED_RECIPIENTS,
        listing_source_urls,
        validate_email_payload,
    )
    from .refresh_state import (
        build_payload_source_identity,
        build_refresh_target_identity,
        parse_utc,
        utc_iso,
    )
except ImportError:
    from audience_guard import (
        CANONICAL_DASHBOARD_URL,
        EXPECTED_RECIPIENTS,
        listing_source_urls,
        validate_email_payload,
    )
    from refresh_state import (
        build_payload_source_identity,
        build_refresh_target_identity,
        parse_utc,
        utc_iso,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = Path("out/run-state.json")
SCHEMA_VERSION = 2
CONTRACT_ROLE = "terminal_summary"
DEFAULT_WORKFLOW_ID = "kegerator-tracker-email"
EXPECTED_LANE_ID = "scheduled-email"
EXPECTED_ORIGIN_REPOSITORY = "lukestambaugh75-hue/kegerator-tracker-r0"
ALLOWED_ORIGIN_URLS = {
    "https://github.com/lukestambaugh75-hue/kegerator-tracker-r0.git",
    "git@github.com:lukestambaugh75-hue/kegerator-tracker-r0.git",
    "ssh://git@github.com/lukestambaugh75-hue/kegerator-tracker-r0.git",
}
STAGE_ORDER = [
    "preflight",
    "freshness",
    "blocker",
    "repair",
    "verification",
    "deployment",
    "payload",
    "receipt",
]
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
STALE_RUN_AGE = timedelta(hours=12)
MAX_PAYLOAD_AGE = timedelta(hours=4)
PYTHON = "/usr/bin/python3"
MAKE = "/usr/bin/make"
REPAIR_ID = "history-prune"


class RunEvidenceError(ValueError):
    """Raised when evidence cannot be proven by the active adapter."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bounded_reason(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "unknown failure")).strip()
    return text[:512]


def _git(
    root: Path,
    *args: str,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        text=text,
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


def _exact_repo_root(root: Path) -> Path:
    root = root.resolve(strict=True)
    if _git_text(root, "rev-parse", "--show-toplevel") != os.fspath(root):
        raise RunEvidenceError("run root must be the exact git repository root")
    return root


def _assert_plain_path(path: Path, *, allow_missing_leaf: bool = False) -> None:
    """Reject symlinks and non-directories in every existing parent component."""
    parts = path.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        current = current / part
        is_leaf = index == len(parts) - 1
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if is_leaf and allow_missing_leaf:
                return
            raise RunEvidenceError(f"evidence path component is missing: {current}")
        if stat.S_ISLNK(mode):
            raise RunEvidenceError(f"evidence paths must not contain symlinks: {current}")
        if not is_leaf and not stat.S_ISDIR(mode):
            raise RunEvidenceError(f"evidence parent is not a directory: {current}")
        if is_leaf and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise RunEvidenceError(f"evidence path must be a regular file or directory: {current}")


def _canonical_out_path(
    root: Path,
    supplied: Path | str,
    relative: Path | str,
    *,
    create_parent: bool = False,
    require_file: bool = False,
) -> Path:
    root = root.resolve(strict=True)
    expected = root / Path(relative)
    candidate = Path(supplied)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    if candidate != expected:
        raise RunEvidenceError(f"evidence path must be exactly {Path(relative).as_posix()}")
    try:
        expected.relative_to(root / "out")
    except ValueError as exc:
        raise RunEvidenceError("evidence must stay under the canonical out directory") from exc

    out_dir = root / "out"
    if out_dir.exists() or out_dir.is_symlink():
        _assert_plain_path(out_dir)
        if not out_dir.is_dir():
            raise RunEvidenceError("out must be a real directory")
    elif create_parent:
        out_dir.mkdir(mode=0o700)
    else:
        raise RunEvidenceError("canonical out directory is unavailable")

    parent = expected.parent
    if create_parent:
        relative_parent = parent.relative_to(out_dir)
        current = out_dir
        for part in relative_parent.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                _assert_plain_path(current)
                if not current.is_dir():
                    raise RunEvidenceError(f"evidence parent is not a directory: {current}")
            else:
                current.mkdir(mode=0o700)
    _assert_plain_path(parent)
    if expected.exists() or expected.is_symlink():
        _assert_plain_path(expected)
        if not expected.is_file():
            raise RunEvidenceError(f"evidence file must be a regular file: {expected}")
    elif require_file:
        raise RunEvidenceError(f"evidence file is unavailable: {Path(relative).as_posix()}")

    rel_text = expected.relative_to(root).as_posix()
    if _git(root, "ls-files", "--error-unmatch", "--", rel_text, check=False).returncode == 0:
        raise RunEvidenceError("evidence path must not be tracked by git")
    if _git(root, "check-ignore", "--quiet", "--", rel_text, check=False).returncode != 0:
        raise RunEvidenceError("evidence path must be ignored by git")
    return expected


def _state_path(root: Path, state_path: Path | str, *, create: bool = False) -> Path:
    return _canonical_out_path(
        root,
        state_path,
        DEFAULT_STATE_PATH,
        create_parent=create,
        require_file=not create,
    )


def _atomic_json(path: Path, value: dict) -> None:
    _assert_plain_path(path.parent)
    if path.exists() or path.is_symlink():
        _assert_plain_path(path)
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


def _validate_origin(value: dict | None, *, allow_unverified: bool) -> None:
    if not isinstance(value, dict) or value.get("status") not in {"verified", "unverified"}:
        raise RunEvidenceError("origin evidence has an invalid schema")
    parse_utc(value.get("observed_at_utc"))
    if value["status"] == "unverified":
        if not allow_unverified or set(value) != {"status", "observed_at_utc", "reason_code"}:
            raise RunEvidenceError("unverified origin evidence is not allowed here")
        return
    required = {
        "status",
        "observed_at_utc",
        "remote_name",
        "repository",
        "fetch_url",
        "push_url",
        "live_main_sha",
    }
    if set(value) != required:
        raise RunEvidenceError("verified origin evidence has an invalid schema")
    if value["remote_name"] != "origin" or value["repository"] != EXPECTED_ORIGIN_REPOSITORY:
        raise RunEvidenceError("origin evidence names the wrong repository")
    if value["fetch_url"] not in ALLOWED_ORIGIN_URLS or value["push_url"] not in ALLOWED_ORIGIN_URLS:
        raise RunEvidenceError("origin evidence contains an unapproved URL")
    if not COMMIT_RE.fullmatch(str(value["live_main_sha"] or "")):
        raise RunEvidenceError("origin evidence contains an invalid main SHA")


def _validate_owner(value: dict) -> None:
    if not isinstance(value, dict) or set(value) != {"hostname", "pid", "process_start_token"}:
        raise RunEvidenceError("run owner has an invalid schema")
    if not isinstance(value["pid"], int) or isinstance(value["pid"], bool) or value["pid"] <= 1:
        raise RunEvidenceError("run owner PID is invalid")
    if not str(value["hostname"] or "") or not str(value["process_start_token"] or ""):
        raise RunEvidenceError("run owner identity is incomplete")


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
        "owner",
        "origin_at_start",
        "origin_at_finish",
        "start_sha",
        "result_sha",
        "status",
        "stage_order",
        "stages",
        "recovery",
    }
    if not isinstance(state, dict) or set(state) != required:
        raise RunEvidenceError("run state has an invalid top-level schema")
    if state["schema_version"] != SCHEMA_VERSION or state["contract_role"] != CONTRACT_ROLE:
        raise RunEvidenceError("run state schema or contract role is unsupported")
    _validate_id(state["run_id"], "run_id")
    _validate_id(state["workflow_id"], "workflow_id")
    _validate_id(state["lane_id"], "lane_id")
    if state["workflow_id"] != DEFAULT_WORKFLOW_ID or state["lane_id"] != EXPECTED_LANE_ID:
        raise RunEvidenceError("run state belongs to an unsupported workflow or lane")
    if not COMMIT_RE.fullmatch(str(state["start_sha"] or "")):
        raise RunEvidenceError("run state start_sha is invalid")
    if state["result_sha"] is not None and not COMMIT_RE.fullmatch(str(state["result_sha"])):
        raise RunEvidenceError("run state result_sha is invalid")
    parse_utc(state["started_at_utc"])
    if state["finished_at_utc"] is not None:
        parse_utc(state["finished_at_utc"])
    _validate_owner(state["owner"])
    _validate_origin(state["origin_at_start"], allow_unverified=False)
    if state["origin_at_finish"] is not None:
        _validate_origin(state["origin_at_finish"], allow_unverified=True)
    if state["status"] not in {"running", "blocked", "failed", "delivery_unverified"}:
        raise RunEvidenceError("run state status is invalid")
    if state["status"] == "running" and state["finished_at_utc"] is not None:
        raise RunEvidenceError("running state cannot have a finish timestamp")
    if state["status"] != "running" and state["finished_at_utc"] is None:
        raise RunEvidenceError("terminal state requires a finish timestamp")
    if state["stage_order"] != STAGE_ORDER:
        raise RunEvidenceError("run state stage_order is invalid")
    if not isinstance(state["stages"], dict) or set(state["stages"]) != set(STAGE_ORDER):
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
        if stage["source_sha"] is not None and not DIGEST_RE.fullmatch(str(stage["source_sha"])):
            raise RunEvidenceError(f"run state stage {name} source_sha is invalid")
        if not isinstance(stage["evidence"], dict):
            raise RunEvidenceError(f"run state stage {name} evidence must be an object")
    if state["recovery"] is not None and not isinstance(state["recovery"], dict):
        raise RunEvidenceError("run recovery evidence must be an object or null")


def _load_state(root: Path, state_path: Path | str) -> tuple[Path, dict]:
    path = _state_path(root, state_path)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"run state is unavailable: {exc}") from exc
    _validate_state_shape(state)
    if Path(state["repo_root"]).resolve() != root.resolve():
        raise RunEvidenceError("run state belongs to a different repository root")
    return path, state


def _require_running(state: dict) -> None:
    if state["status"] != "running" or state["finished_at_utc"] is not None:
        raise RunEvidenceError("run state is already finished")


def _read_repo_json(root: Path, relative: str) -> object:
    path = root / relative
    _assert_plain_path(path)
    if _git(root, "ls-files", "--error-unmatch", "--", relative, check=False).returncode != 0:
        raise RunEvidenceError(f"required source is not tracked: {relative}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"current source is invalid: {relative}: {exc}") from exc


def _load_sources(root: Path) -> tuple[list[dict], list[dict], dict, dict]:
    listings = _read_repo_json(root, "data/listings.json")
    specs = _read_repo_json(root, "data/specs.json")
    refresh = _read_repo_json(root, "data/refresh-status.json")
    if not isinstance(listings, list) or not isinstance(specs, list) or not isinstance(refresh, dict):
        raise RunEvidenceError("current refresh inputs have invalid container types")
    identity = build_payload_source_identity(listings, specs, refresh)
    return listings, specs, refresh, identity


def _pid_start_token(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
    )
    token = " ".join(result.stdout.split())
    return token or None


def _read_live_origin(root: Path, observed_at: datetime) -> dict:
    fetch_url = _git_text(root, "remote", "get-url", "origin")
    push_url = _git_text(root, "remote", "get-url", "--push", "origin")
    if fetch_url not in ALLOWED_ORIGIN_URLS or push_url not in ALLOWED_ORIGIN_URLS:
        raise RunEvidenceError("origin URL does not identify the approved Kegerator repository")
    result = _git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main", check=False)
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) != 1 or len(lines[0]) != 2:
        raise RunEvidenceError("live origin/main identity is unavailable or ambiguous")
    sha, ref = lines[0]
    if ref != "refs/heads/main" or not COMMIT_RE.fullmatch(sha):
        raise RunEvidenceError("live origin/main identity is invalid")
    return {
        "status": "verified",
        "observed_at_utc": utc_iso(observed_at),
        "remote_name": "origin",
        "repository": EXPECTED_ORIGIN_REPOSITORY,
        "fetch_url": fetch_url,
        "push_url": push_url,
        "live_main_sha": sha,
    }


OriginReader = Callable[[Path, datetime], dict]


def _same_origin_repository(start: dict, current: dict) -> None:
    for field in ("repository", "fetch_url", "push_url"):
        if start.get(field) != current.get(field):
            raise RunEvidenceError(f"origin {field} changed during the run")


def _archive_terminal_state(root: Path, state_path: Path, state: dict) -> None:
    raw = state_path.read_bytes()
    compact_time = re.sub(r"[^0-9]", "", state["started_at_utc"])
    name = f"{state['run_id']}-{compact_time}-{_sha256(raw)[:12]}.json"
    relative = Path("out/run-state-archive") / name
    destination = _canonical_out_path(
        root,
        root / relative,
        relative,
        create_parent=True,
    )
    if destination.exists():
        if destination.read_bytes() != raw:
            raise RunEvidenceError("terminal run archive collision requires review")
        return
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError("terminal run cannot be archived safely") from exc
    _atomic_json(destination, value)


def create_run_state(
    root: Path,
    state_path: Path | str,
    run_id: str,
    workflow_id: str,
    lane_id: str,
    *,
    owner_pid: int | None = None,
    now: datetime | None = None,
    origin_reader: OriginReader = _read_live_origin,
) -> dict:
    root = _exact_repo_root(root)
    path = _state_path(root, state_path, create=True)
    run_id = _validate_id(run_id, "run_id")
    workflow_id = _validate_id(workflow_id, "workflow_id")
    lane_id = _validate_id(lane_id, "lane_id")
    if workflow_id != DEFAULT_WORKFLOW_ID or lane_id != EXPECTED_LANE_ID:
        raise RunEvidenceError("only the canonical scheduled Kegerator lane is supported")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunEvidenceError("existing run state is invalid; preserve it for review") from exc
        _validate_state_shape(previous)
        if previous["status"] == "running":
            raise RunEvidenceError("an unfinished run state already occupies this lane")
        _archive_terminal_state(root, path, previous)

    owner_pid = int(owner_pid or os.getpid())
    owner_token = _pid_start_token(owner_pid)
    if owner_pid <= 1 or owner_token is None:
        raise RunEvidenceError("run owner process is not currently live")
    if _git_text(root, "branch", "--show-current") != "main":
        raise RunEvidenceError("automation run must start on main")
    if _git_text(root, "status", "--porcelain", "--untracked-files=no"):
        raise RunEvidenceError("tracked worktree changes must be resolved before the run starts")
    start_sha = _git_text(root, "rev-parse", "HEAD")
    origin = origin_reader(root, observed_at)
    _validate_origin(origin, allow_unverified=False)
    if origin["live_main_sha"] != start_sha:
        raise RunEvidenceError("local main must match live origin/main before the run starts")
    listings, _, _, start_identity = _load_sources(root)
    listing_source_urls(listings)
    for relative in ("history.csv", "Makefile", "scripts/refresh.py", "scripts/repair_history.py"):
        fixed_path = root / relative
        _assert_plain_path(fixed_path)
        if not fixed_path.is_file():
            raise RunEvidenceError(f"fixed run dependency must be a regular file: {relative}")
        if _git(root, "ls-files", "--error-unmatch", "--", relative, check=False).returncode != 0:
            raise RunEvidenceError(f"fixed run dependency must be tracked by git: {relative}")
    target_identity = build_refresh_target_identity(listings)
    source_sha = start_identity["source_sha256"]
    owner = {
        "hostname": socket.gethostname(),
        "pid": owner_pid,
        "process_start_token": owner_token,
    }
    state = {
        "schema_version": SCHEMA_VERSION,
        "contract_role": CONTRACT_ROLE,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "lane_id": lane_id,
        "repo_root": os.fspath(root),
        "started_at_utc": utc_iso(observed_at),
        "finished_at_utc": None,
        "owner": owner,
        "origin_at_start": origin,
        "origin_at_finish": None,
        "start_sha": start_sha,
        "result_sha": None,
        "status": "running",
        "stage_order": list(STAGE_ORDER),
        "stages": {
            "preflight": _stage(
                "passed",
                observed_at,
                run_id,
                source_sha,
                {
                    "branch": "main",
                    "start_sha": start_sha,
                    "tracked_worktree_clean": True,
                    "owner": owner,
                    "origin": origin,
                    "target_identity": target_identity,
                },
            ),
            "freshness": _stage("pending", observed_at, run_id, source_sha),
            "blocker": _stage("clear", observed_at, run_id, source_sha),
            "repair": _stage(
                "not_required",
                observed_at,
                run_id,
                source_sha,
                {"repair_id": None, "action": None, "attempts_used": 0, "max_attempts": 1},
            ),
            "verification": _stage(
                "pending", observed_at, run_id, source_sha, {"attempts": []}
            ),
            "deployment": _stage("unverified", observed_at, run_id, None),
            "payload": _stage("unverified", observed_at, run_id, None),
            "receipt": _stage(
                "unverified",
                observed_at,
                run_id,
                None,
                {"reason_code": "trusted_receipt_adapter_unavailable"},
            ),
        },
        "recovery": None,
    }
    _validate_state_shape(state)
    _atomic_json(path, state)
    return state


def _command_result(result: subprocess.CompletedProcess) -> tuple[int, bytes, bytes]:
    stdout = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout or "").encode()
    stderr = result.stderr if isinstance(result.stderr, bytes) else str(result.stderr or "").encode()
    return int(result.returncode), stdout, stderr


def _execute(
    command: list[str],
    root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[int, bytes, bytes]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = runner(
        command,
        cwd=os.fspath(root),
        env=env,
        capture_output=True,
        check=False,
    )
    return _command_result(result)


def _record_blocker(
    state: dict,
    observed_at: datetime,
    source_sha: str | None,
    *,
    stage: str,
    reason_code: str,
    detail: object,
) -> None:
    state["stages"]["blocker"] = _stage(
        "recorded",
        observed_at,
        state["run_id"],
        source_sha,
        {
            "failure_stage": stage,
            "reason_code": reason_code,
            "detail": _bounded_reason(detail),
        },
    )


def execute_refresh_once(
    root: Path,
    state_path: Path | str,
    *,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["freshness"]["status"] != "pending":
        raise RunEvidenceError("the run-bound refresh may execute exactly once")
    started = parse_utc(now or _now())
    assert started is not None
    listings_before, _, _, identity_before = _load_sources(root)
    target_before = build_refresh_target_identity(listings_before)
    expected_target = state["stages"]["preflight"]["evidence"]["target_identity"]
    if target_before != expected_target:
        raise RunEvidenceError("refresh targets drifted after run preflight")
    relative = Path("out/runs") / state["run_id"] / "refresh-outcome.json"
    outcome_path = _canonical_out_path(
        root,
        root / relative,
        relative,
        create_parent=True,
    )
    if outcome_path.exists():
        raise RunEvidenceError("canonical refresh outcome already exists for this run_id")
    script = root / "scripts/refresh.py"
    _assert_plain_path(script)
    command = [
        PYTHON,
        os.fspath(script),
        "--outcome-path",
        os.fspath(outcome_path),
        "--run-id",
        state["run_id"],
    ]
    invocation = {
        "command": command,
        "cwd": os.fspath(root),
        "script_sha256": _sha256(script.read_bytes()),
        "started_at_utc": utc_iso(started),
        "input_source_sha256": identity_before["source_sha256"],
        "target_identity": target_before,
    }
    state["stages"]["freshness"] = _stage(
        "in_progress", started, state["run_id"], identity_before["source_sha256"], invocation
    )
    _atomic_json(path, state)
    exit_code, stdout, stderr = _execute(command, root, runner=runner)
    finished = parse_utc(now or _now())
    assert finished is not None
    invocation.update(
        {
            "finished_at_utc": utc_iso(finished),
            "exit_code": exit_code,
            "stdout_sha256": _sha256(stdout),
            "stderr_sha256": _sha256(stderr),
        }
    )
    if exit_code != 0:
        state["stages"]["freshness"] = _stage(
            "failed", finished, state["run_id"], identity_before["source_sha256"], invocation
        )
        _record_blocker(
            state,
            finished,
            identity_before["source_sha256"],
            stage="freshness",
            reason_code="refresh_command_failed",
            detail=f"fixed refresh command exited {exit_code}",
        )
        _atomic_json(path, state)
        raise RunEvidenceError(f"fixed refresh command exited {exit_code}")

    outcome_path = _canonical_out_path(
        root,
        outcome_path,
        relative,
        require_file=True,
    )
    raw = outcome_path.read_bytes()
    try:
        outcome = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"run-bound refresh outcome is invalid: {exc}") from exc
    required_outcome = {
        "status",
        "reason",
        "attempted_at_utc",
        "expected_count",
        "confirmed_count",
        "failed_count",
        "history_appended",
        "run_id",
        "input_source_count",
        "target_manifest_sha256",
    }
    if not isinstance(outcome, dict) or set(outcome) != required_outcome:
        raise RunEvidenceError("run-bound refresh outcome schema is not exact")
    if outcome["run_id"] != state["run_id"]:
        raise RunEvidenceError("refresh outcome belongs to a different run")
    if outcome["input_source_count"] != target_before["source_count"]:
        raise RunEvidenceError("refresh outcome source count differs from actual run targets")
    if outcome["target_manifest_sha256"] != target_before["target_manifest_sha256"]:
        raise RunEvidenceError("refresh outcome target manifest differs from actual run targets")
    attempted_at = parse_utc(outcome["attempted_at_utc"])
    if attempted_at is None or attempted_at < parse_utc(state["started_at_utc"]):
        raise RunEvidenceError("refresh outcome does not belong to this run window")
    if attempted_at > finished + timedelta(minutes=5):
        raise RunEvidenceError("refresh outcome timestamp is in the future")
    status = str(outcome["status"] or "").casefold()
    if status not in {"success", "blocked", "partial", "failed"}:
        raise RunEvidenceError("refresh outcome status is invalid")
    counts = [outcome.get(name) for name in ("expected_count", "confirmed_count", "failed_count")]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in counts):
        raise RunEvidenceError("refresh outcome counts must be integers")
    expected_count, confirmed_count, failed_count = counts
    if (
        expected_count != target_before["source_count"]
        or expected_count <= 0
        or confirmed_count < 0
        or failed_count < 0
        or confirmed_count + failed_count != expected_count
    ):
        raise RunEvidenceError("refresh outcome counts do not match the actual target inventory")
    if not isinstance(outcome["history_appended"], int) or isinstance(outcome["history_appended"], bool):
        raise RunEvidenceError("refresh history count is invalid")

    listings, specs, refresh, identity = _load_sources(root)
    target_after = build_refresh_target_identity(listings)
    if target_after != target_before:
        raise RunEvidenceError("refresh changed the configured target inventory")
    if refresh.get("last_attempt_at_utc") != utc_iso(attempted_at):
        raise RunEvidenceError("stored refresh attempt does not match the run outcome")
    if refresh.get("last_attempt_status") != status:
        raise RunEvidenceError("stored refresh status does not match the run outcome")
    if status == "success":
        if confirmed_count != expected_count or failed_count != 0:
            raise RunEvidenceError("successful refresh did not confirm every actual target")
        if refresh.get("data_refreshed_at_utc") != utc_iso(attempted_at):
            raise RunEvidenceError("successful snapshot does not match this refresh attempt")
        freshness_status = "passed"
        blocker_status = "clear"
        blocker_evidence: dict = {}
    else:
        reason = str(outcome.get("reason") or "").strip()
        if not reason or refresh.get("last_attempt_reason") != reason:
            raise RunEvidenceError("non-successful refresh lacks the matching blocker reason")
        if status in {"blocked", "failed"} and (confirmed_count != 0 or failed_count != expected_count):
            raise RunEvidenceError(f"{status} refresh counts are incoherent")
        if status == "partial" and not (
            0 < confirmed_count < expected_count and failed_count == expected_count - confirmed_count
        ):
            raise RunEvidenceError("partial refresh counts are incoherent")
        freshness_status = "blocked"
        blocker_status = "recorded"
        blocker_evidence = {
            "failure_stage": "freshness",
            "reason_code": f"refresh_{status}",
            "detail": _bounded_reason(reason),
        }
    invocation.update(
        {
            "outcome_file": relative.as_posix(),
            "outcome_sha256": _sha256(raw),
            "outcome": outcome,
            "output_source_identity": identity,
            "output_target_identity": target_after,
            "listing_count": len(listings),
            "spec_count": len(specs),
        }
    )
    source_sha = identity["source_sha256"]
    state["stages"]["freshness"] = _stage(
        freshness_status, finished, state["run_id"], source_sha, invocation
    )
    state["stages"]["blocker"] = _stage(
        blocker_status, finished, state["run_id"], source_sha, blocker_evidence
    )
    state["stages"]["repair"] = _stage(
        "not_required",
        finished,
        state["run_id"],
        source_sha,
        {"repair_id": None, "action": None, "attempts_used": 0, "max_attempts": 1},
    )
    state["stages"]["verification"] = _stage(
        "pending", finished, state["run_id"], source_sha, {"attempts": []}
    )
    _atomic_json(path, state)
    return state


def run_local_verification(
    root: Path,
    state_path: Path | str,
    *,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["freshness"]["status"] not in {"passed", "blocked"}:
        raise RunEvidenceError("local verification requires completed refresh evidence")
    verification = state["stages"]["verification"]
    attempts = list(verification["evidence"].get("attempts") or [])
    if verification["status"] == "passed":
        raise RunEvidenceError("local verification is already current")
    if attempts and state["stages"]["repair"]["status"] != "passed":
        raise RunEvidenceError("verification may rerun only after the one bounded repair")
    if len(attempts) >= 2:
        raise RunEvidenceError("verification attempt budget is exhausted")
    observed_start = parse_utc(now or _now())
    assert observed_start is not None
    _, _, _, identity_before = _load_sources(root)
    source_sha = identity_before["source_sha256"]
    command = [MAKE, "verify-current"]
    attempt = {
        "attempt": len(attempts) + 1,
        "command": command,
        "cwd": os.fspath(root),
        "makefile_sha256": _sha256((root / "Makefile").read_bytes()),
        "started_at_utc": utc_iso(observed_start),
        "input_source_sha256": source_sha,
        "after_repair": state["stages"]["repair"]["status"] == "passed",
    }
    exit_code, stdout, stderr = _execute(command, root, runner=runner)
    observed_finish = parse_utc(now or _now())
    assert observed_finish is not None
    _, _, _, identity_after = _load_sources(root)
    if identity_after != identity_before:
        exit_code = 98
    attempt.update(
        {
            "finished_at_utc": utc_iso(observed_finish),
            "exit_code": exit_code,
            "stdout_sha256": _sha256(stdout),
            "stderr_sha256": _sha256(stderr),
            "output_source_sha256": identity_after["source_sha256"],
        }
    )
    attempts.append(attempt)
    status = "passed" if exit_code == 0 and identity_after == identity_before else "failed"
    state["stages"]["verification"] = _stage(
        status,
        observed_finish,
        state["run_id"],
        source_sha,
        {"attempts": attempts},
    )
    if status == "failed":
        _record_blocker(
            state,
            observed_finish,
            source_sha,
            stage="verification",
            reason_code="fixed_verification_failed",
            detail=f"fixed verification command exited {exit_code}",
        )
    elif state["stages"]["freshness"]["status"] == "passed":
        state["stages"]["blocker"] = _stage(
            "clear", observed_finish, state["run_id"], source_sha
        )
    else:
        outcome = state["stages"]["freshness"]["evidence"].get("outcome") or {}
        _record_blocker(
            state,
            observed_finish,
            source_sha,
            stage="freshness",
            reason_code=f"refresh_{outcome.get('status', 'blocked')}",
            detail=outcome.get("reason") or "refresh did not produce a complete current snapshot",
        )
    _atomic_json(path, state)
    if status != "passed":
        raise RunEvidenceError(f"fixed verification command exited {exit_code}")
    return state


def propose_repair(
    root: Path,
    state_path: Path | str,
    repair_id: str,
    *,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    repair_id = _validate_id(repair_id, "repair_id")
    if repair_id != REPAIR_ID:
        raise RunEvidenceError("only the allowlisted history-prune repair is supported")
    if state["stages"]["verification"]["status"] != "failed":
        raise RunEvidenceError("repair requires a recorded failed local verification")
    if state["stages"]["repair"]["status"] != "not_required":
        raise RunEvidenceError("only one bounded repair may be proposed per run")
    history = root / "history.csv"
    tool = root / "scripts/repair_history.py"
    _assert_plain_path(history)
    _assert_plain_path(tool)
    command = [PYTHON, os.fspath(tool), "--path", os.fspath(history), "--check"]
    exit_code, stdout, stderr = _execute(command, root, runner=runner)
    match = re.fullmatch(rb"(\d+) kept, (\d+) would remove\s*", stdout)
    if exit_code != 1 or not match or int(match.group(2)) <= 0:
        raise RunEvidenceError("history-prune precheck did not prove one applicable repair")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    source_sha = state["stages"]["freshness"]["source_sha"]
    state["stages"]["repair"] = _stage(
        "proposed",
        observed_at,
        state["run_id"],
        source_sha,
        {
            "repair_id": repair_id,
            "action": "remove_only_estimated_history_rows",
            "target_path": "history.csv",
            "target_sha256_before": _sha256(history.read_bytes()),
            "tool_path": "scripts/repair_history.py",
            "tool_sha256": _sha256(tool.read_bytes()),
            "precheck_command": command,
            "precheck_exit_code": exit_code,
            "precheck_stdout_sha256": _sha256(stdout),
            "precheck_stderr_sha256": _sha256(stderr),
            "kept_count": int(match.group(1)),
            "remove_count": int(match.group(2)),
            "max_attempts": 1,
            "attempts_used": 0,
        },
    )
    state["stages"]["verification"] = _stage(
        "pending",
        observed_at,
        state["run_id"],
        source_sha,
        state["stages"]["verification"]["evidence"],
    )
    _atomic_json(path, state)
    return state


def execute_repair(
    root: Path,
    state_path: Path | str,
    repair_id: str,
    *,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    repair = state["stages"]["repair"]
    if repair_id != REPAIR_ID or repair["status"] != "proposed":
        raise RunEvidenceError("repair attempt requires the matching allowlisted proposal")
    evidence = repair["evidence"]
    if evidence.get("attempts_used") != 0 or evidence.get("max_attempts") != 1:
        raise RunEvidenceError("the bounded repair attempt has already been consumed")
    history = root / "history.csv"
    tool = root / "scripts/repair_history.py"
    if _sha256(history.read_bytes()) != evidence["target_sha256_before"]:
        raise RunEvidenceError("repair target drifted after proposal")
    if _sha256(tool.read_bytes()) != evidence["tool_sha256"]:
        raise RunEvidenceError("repair tool drifted after proposal")
    command = [PYTHON, os.fspath(tool), "--path", os.fspath(history)]
    exit_code, stdout, stderr = _execute(command, root, runner=runner)
    check_command = [PYTHON, os.fspath(tool), "--path", os.fspath(history), "--check"]
    check_exit, check_stdout, check_stderr = _execute(check_command, root, runner=runner)
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    result_evidence = {
        **evidence,
        "attempts_used": 1,
        "repair_command": command,
        "repair_exit_code": exit_code,
        "repair_stdout_sha256": _sha256(stdout),
        "repair_stderr_sha256": _sha256(stderr),
        "target_sha256_after": _sha256(history.read_bytes()),
        "postcheck_command": check_command,
        "postcheck_exit_code": check_exit,
        "postcheck_stdout_sha256": _sha256(check_stdout),
        "postcheck_stderr_sha256": _sha256(check_stderr),
    }
    status = "passed" if exit_code == 0 and check_exit == 0 else "failed"
    state["stages"]["repair"] = _stage(
        status,
        observed_at,
        state["run_id"],
        repair["source_sha"],
        result_evidence,
    )
    state["stages"]["verification"] = _stage(
        "pending",
        observed_at,
        state["run_id"],
        repair["source_sha"],
        state["stages"]["verification"]["evidence"],
    )
    if status == "failed":
        _record_blocker(
            state,
            observed_at,
            repair["source_sha"],
            stage="repair",
            reason_code="bounded_repair_failed",
            detail=f"repair exit={exit_code}; postcheck exit={check_exit}",
        )
    _atomic_json(path, state)
    if status != "passed":
        raise RunEvidenceError("bounded history repair did not pass its fixed postcheck")
    return state


def record_result_sha(
    root: Path,
    state_path: Path | str,
    *,
    now: datetime | None = None,
    origin_reader: OriginReader = _read_live_origin,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["freshness"]["status"] not in {"passed", "blocked"}:
        raise RunEvidenceError("refresh evidence must be recorded before result_sha")
    if state["stages"]["verification"]["status"] != "passed":
        raise RunEvidenceError("recorded local verification must pass before result_sha")
    if state["stages"]["repair"]["status"] in {"proposed", "failed"}:
        raise RunEvidenceError("result_sha cannot bind an incomplete or failed repair")
    if _git_text(root, "branch", "--show-current") != "main":
        raise RunEvidenceError("result must remain on main")
    if _git_text(root, "status", "--porcelain", "--untracked-files=no"):
        raise RunEvidenceError("result_sha cannot bind an uncommitted tracked worktree")
    result_sha = _git_text(root, "rev-parse", "HEAD")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    origin = origin_reader(root, observed_at)
    _validate_origin(origin, allow_unverified=False)
    _same_origin_repository(state["origin_at_start"], origin)
    if origin["live_main_sha"] != result_sha:
        raise RunEvidenceError("result_sha must match live origin/main")
    current_identity = _load_sources(root)[3]
    recorded_identity = state["stages"]["freshness"]["evidence"].get("output_source_identity")
    if current_identity != recorded_identity:
        raise RunEvidenceError("result inputs drifted after refresh evidence was recorded")
    state["result_sha"] = result_sha
    state["stages"]["preflight"]["evidence"]["result_bound_at_utc"] = utc_iso(observed_at)
    state["stages"]["preflight"]["evidence"]["result_origin"] = origin
    _atomic_json(path, state)
    return state


def record_deployment_evidence(
    root: Path,
    state_path: Path | str,
    *,
    now: datetime | None = None,
    verifier: Callable[..., tuple[dict, dict]] | None = None,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if not state["result_sha"]:
        raise RunEvidenceError("result_sha must be bound before deployment verification")
    if state["stages"]["freshness"]["status"] != "passed":
        raise RunEvidenceError("deployment cannot pass for a non-successful refresh")
    if verifier is None:
        from scripts.check_public_pages import verify_public_deployment

        verifier = verify_public_deployment
    public_state, proof = verifier(root, expected_sha=state["result_sha"])
    if not isinstance(public_state, dict) or not isinstance(proof, dict):
        raise RunEvidenceError("deployment verifier returned invalid evidence")
    if public_state.get("state") != "Fresh":
        raise RunEvidenceError("public deployment is not a fresh current snapshot")
    required = {
        "result_sha",
        "origin_main_sha",
        "source_sha",
        "bundle_marker_sha256",
        "files",
        "public_url",
        "public_state",
        "data_refreshed_at_utc",
        "fetches",
        "origin",
    }
    if set(proof) != required:
        raise RunEvidenceError("deployment verifier proof schema is not exact")
    if proof["result_sha"] != state["result_sha"] or proof["origin_main_sha"] != state["result_sha"]:
        raise RunEvidenceError("deployment proof is not bound to this run result")
    source_sha = state["stages"]["freshness"]["source_sha"]
    if proof["source_sha"] != source_sha:
        raise RunEvidenceError("deployment proof source identity differs from this run")
    if proof["public_url"] != CANONICAL_DASHBOARD_URL:
        raise RunEvidenceError("deployment proof names a non-canonical public URL")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
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
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["deployment"]["status"] != "passed":
        raise RunEvidenceError("deployment must be proven before payload evidence is accepted")
    payload_file = _canonical_out_path(
        root,
        payload_path,
        Path("out/latest-email.json"),
        require_file=True,
    )
    raw = payload_file.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"email payload evidence is invalid: {exc}") from exc
    listings, specs, refresh, current_identity = _load_sources(root)
    recorded_identity = state["stages"]["freshness"]["evidence"].get("output_source_identity")
    if current_identity != recorded_identity:
        raise RunEvidenceError("email payload inputs drifted after the current refresh")
    generated_at = parse_utc(payload.get("generated_at") if isinstance(payload, dict) else None)
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    attempted_at = parse_utc(current_identity["last_attempt_at_utc"])
    if generated_at is None or attempted_at is None:
        raise RunEvidenceError("email payload timestamps are incomplete")
    if generated_at < attempted_at or generated_at < parse_utc(state["started_at_utc"]):
        raise RunEvidenceError("email payload predates this run refresh")
    if generated_at > observed_at + timedelta(minutes=5):
        raise RunEvidenceError("email payload timestamp is in the future")
    if observed_at - generated_at > MAX_PAYLOAD_AGE:
        raise RunEvidenceError("email payload is too old for this delivery attempt")
    from tools.build_email import build_payload, serialize_payload

    expected = build_payload(listings, specs, refresh, now=generated_at)
    if payload != expected or raw != serialize_payload(expected):
        raise RunEvidenceError("email payload is not the exact deterministic current payload")
    validate_email_payload(
        payload,
        listing_source_urls(listings),
        expected_source_identity=build_payload_source_identity(listings, specs, refresh),
    )
    if payload["refresh_state"] != "Fresh":
        raise RunEvidenceError("email payload is not fresh enough for delivery")
    state["stages"]["payload"] = _stage(
        "passed",
        observed_at,
        state["run_id"],
        current_identity["source_sha256"],
        {
            "payload_sha256": _sha256(raw),
            "payload_file": "out/latest-email.json",
            "generated_at_utc": utc_iso(generated_at),
            "to": payload["to"],
            "cc": payload["cc"],
            "bcc": payload["bcc"],
            "subject": payload["subject"],
            "source_identity": current_identity,
        },
    )
    _atomic_json(path, state)
    return state


def record_receipt_evidence(*args, **kwargs) -> dict:
    """Fail closed: detached JSON is not trusted external delivery evidence."""
    raise RunEvidenceError(
        "no trusted external receipt adapter is installed; receipt remains unverified"
    )


def _finish_origin(
    root: Path,
    state: dict,
    observed_at: datetime,
    origin_reader: OriginReader,
    *,
    allow_unverified: bool,
) -> dict:
    try:
        origin = origin_reader(root, observed_at)
        _validate_origin(origin, allow_unverified=False)
        _same_origin_repository(state["origin_at_start"], origin)
        if state["result_sha"] and origin["live_main_sha"] != state["result_sha"]:
            raise RunEvidenceError("live origin/main no longer matches the run result")
        return origin
    except Exception:
        if not allow_unverified:
            raise
        return {
            "status": "unverified",
            "observed_at_utc": utc_iso(observed_at),
            "reason_code": "live_origin_check_failed",
        }


def finish_run(
    root: Path,
    state_path: Path | str,
    outcome: str,
    *,
    now: datetime | None = None,
    origin_reader: OriginReader = _read_live_origin,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    outcome = str(outcome or "").casefold()
    if outcome not in {"blocked", "failed", "delivery_unverified"}:
        raise RunEvidenceError("delivered is unsupported without a trusted receipt adapter")
    if outcome in {"blocked", "delivery_unverified"} and not state["result_sha"]:
        raise RunEvidenceError(f"{outcome} run requires a clean pushed result_sha")
    if outcome == "blocked":
        if state["stages"]["blocker"]["status"] != "recorded":
            raise RunEvidenceError("blocked run requires recorded blocker evidence")
        if state["stages"]["receipt"]["status"] != "unverified":
            raise RunEvidenceError("blocked run cannot claim a delivery receipt")
    if outcome == "delivery_unverified":
        for stage_name in ("freshness", "verification", "deployment", "payload"):
            if state["stages"][stage_name]["status"] != "passed":
                raise RunEvidenceError(f"delivery_unverified requires {stage_name}=passed")
        if state["stages"]["receipt"]["status"] != "unverified":
            raise RunEvidenceError("delivery_unverified must retain receipt=unverified")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    if observed_at < parse_utc(state["started_at_utc"]):
        raise RunEvidenceError("finish timestamp predates run start")
    state["origin_at_finish"] = _finish_origin(
        root,
        state,
        observed_at,
        origin_reader,
        allow_unverified=outcome == "failed",
    )
    state["finished_at_utc"] = utc_iso(observed_at)
    state["status"] = outcome
    _atomic_json(path, state)
    return state


def finalize_failure(
    root: Path,
    state_path: Path | str,
    failure_stage: str,
    reason_code: str,
    *,
    detail: object = "fixed command failed",
    now: datetime | None = None,
    origin_reader: OriginReader = _read_live_origin,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    failure_stage = _validate_id(failure_stage, "failure_stage")
    reason_code = _validate_id(reason_code, "reason_code")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    source_sha = state["stages"]["freshness"]["source_sha"]
    _record_blocker(
        state,
        observed_at,
        source_sha,
        stage=failure_stage,
        reason_code=reason_code,
        detail=detail,
    )
    state["origin_at_finish"] = _finish_origin(
        root, state, observed_at, origin_reader, allow_unverified=True
    )
    state["finished_at_utc"] = utc_iso(observed_at)
    state["status"] = "failed"
    _atomic_json(path, state)
    return state


def recover_stale_run(
    root: Path,
    state_path: Path | str,
    expected_run_id: str,
    *,
    now: datetime | None = None,
    origin_reader: OriginReader = _read_live_origin,
    owner_probe: Callable[[int], str | None] = _pid_start_token,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["run_id"] != _validate_id(expected_run_id, "expected_run_id"):
        raise RunEvidenceError("stale recovery run_id does not match the occupied lane")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    started_at = parse_utc(state["started_at_utc"])
    assert started_at is not None
    if observed_at - started_at < STALE_RUN_AGE:
        raise RunEvidenceError("run is too recent for governed stale recovery")
    owner = state["owner"]
    if owner["hostname"] != socket.gethostname():
        raise RunEvidenceError("stale recovery cannot prove an owner on another host is dead")
    if owner_probe(owner["pid"]) == owner["process_start_token"]:
        raise RunEvidenceError("run owner process is still live")
    origin = origin_reader(root, observed_at)
    _validate_origin(origin, allow_unverified=False)
    _same_origin_repository(state["origin_at_start"], origin)
    state["recovery"] = {
        "status": "stale_owner_recovered",
        "observed_at_utc": utc_iso(observed_at),
        "expected_run_id": expected_run_id,
        "minimum_age_seconds": int(STALE_RUN_AGE.total_seconds()),
        "age_seconds": int((observed_at - started_at).total_seconds()),
        "owner": owner,
        "origin": origin,
    }
    _record_blocker(
        state,
        observed_at,
        state["stages"]["freshness"]["source_sha"],
        stage="stale_recovery",
        reason_code="dead_owner_after_minimum_age",
        detail="governed recovery closed a stale run without claiming completion",
    )
    state["origin_at_finish"] = origin
    state["finished_at_utc"] = utc_iso(observed_at)
    state["status"] = "failed"
    _atomic_json(path, state)
    return state


def _auto_finalize_cli_failure(root: Path, state_path: Path | str, command: str, exc: Exception) -> None:
    if command in {"start", "recover-stale", "finalize-failure"}:
        return
    if command == "verify":
        try:
            _, state = _load_state(_exact_repo_root(root), state_path)
            attempts = state["stages"]["verification"]["evidence"].get("attempts") or []
            first_repairable_failure = (
                state["stages"]["verification"]["status"] == "failed"
                and len(attempts) == 1
                and state["stages"]["repair"]["status"] == "not_required"
            )
            if first_repairable_failure:
                return
        except Exception:
            return
    try:
        finalize_failure(
            root,
            state_path,
            command,
            "adapter_command_failed",
            detail=f"{type(exc).__name__}: {_bounded_reason(exc)}",
        )
    except Exception:
        pass


def _print_summary(state: dict) -> None:
    print(
        f"run_id={state['run_id']} workflow_id={state['workflow_id']} "
        f"lane_id={state['lane_id']} status={state['status']} "
        f"start_sha={state['start_sha']} result_sha={state['result_sha'] or 'unbound'} "
        f"receipt={state['stages']['receipt']['status']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--run-id", required=True)
    start.add_argument("--workflow-id", default=DEFAULT_WORKFLOW_ID)
    start.add_argument("--lane-id", default=EXPECTED_LANE_ID)
    start.add_argument("--owner-pid", type=int, required=True)
    commands.add_parser("refresh")
    commands.add_parser("verify")
    propose = commands.add_parser("propose-repair")
    propose.add_argument("--repair-id", choices=(REPAIR_ID,), required=True)
    repair = commands.add_parser("repair")
    repair.add_argument("--repair-id", choices=(REPAIR_ID,), required=True)
    commands.add_parser("result")
    commands.add_parser("deployment")
    payload = commands.add_parser("payload")
    payload.add_argument("--payload", type=Path, default=Path("out/latest-email.json"))
    finish = commands.add_parser("finish")
    finish.add_argument(
        "--outcome", choices=("blocked", "failed", "delivery_unverified"), required=True
    )
    failure = commands.add_parser("finalize-failure")
    failure.add_argument("--stage", required=True)
    failure.add_argument("--reason-code", required=True)
    recover = commands.add_parser("recover-stale")
    recover.add_argument("--expected-run-id", required=True)
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
                owner_pid=args.owner_pid,
            )
        elif args.command == "refresh":
            state = execute_refresh_once(root, args.state)
        elif args.command == "verify":
            state = run_local_verification(root, args.state)
        elif args.command == "propose-repair":
            state = propose_repair(root, args.state, args.repair_id)
        elif args.command == "repair":
            state = execute_repair(root, args.state, args.repair_id)
        elif args.command == "result":
            state = record_result_sha(root, args.state)
        elif args.command == "deployment":
            state = record_deployment_evidence(root, args.state)
        elif args.command == "payload":
            state = record_payload_evidence(root, args.state, args.payload)
        elif args.command == "finish":
            state = finish_run(root, args.state, args.outcome)
        elif args.command == "finalize-failure":
            state = finalize_failure(
                root,
                args.state,
                args.stage,
                args.reason_code,
            )
        else:
            state = recover_stale_run(root, args.state, args.expected_run_id)
    except Exception as exc:
        _auto_finalize_cli_failure(root, args.state, args.command, exc)
        print(f"run evidence rejected: {exc}", file=os.sys.stderr)
        return 1
    _print_summary(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
