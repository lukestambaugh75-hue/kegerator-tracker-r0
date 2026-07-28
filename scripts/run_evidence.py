#!/usr/bin/env python3
"""Execute and record one fail-closed Kegerator automation run.

The adapter owns the commands whose evidence it records. It never commits,
pushes, deploys, opens a browser, or sends mail. Delivery remains unverified
until this repository has a trusted external receipt adapter.
"""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_MODULE_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""} and os.fspath(_MODULE_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(_MODULE_ROOT))

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


ROOT = _MODULE_ROOT
DEFAULT_STATE_PATH = Path("out/run-state.json")
SCHEMA_VERSION = 4
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
    "pre_send",
    "receipt",
]
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
STALE_RUN_AGE = timedelta(hours=12)
MAX_PAYLOAD_AGE = timedelta(hours=4)
MAX_PRE_SEND_BIND_AGE = timedelta(minutes=5)
MAX_FINISH_AFTER_PRE_SEND = timedelta(minutes=30)
PYTHON = "/usr/bin/python3"
MAKE = "/usr/bin/make"
REPAIR_ID = "history-prune"
ALLOWED_RESULT_PATHS = {
    "data/listings.json",
    "data/refresh-status.json",
    "history.csv",
}
FIXED_COMMAND_PATHS = {
    ".gitignore",
    "Makefile",
    "scripts/audience_guard.py",
    "scripts/check_public_pages.py",
    "scripts/refresh.py",
    "scripts/refresh_state.py",
    "scripts/repair_history.py",
    "scripts/run_evidence.py",
    "tests/test_run_evidence.py",
    "tests/test_tracker.py",
    "tools/build_email.py",
}
STAGE_STATUSES = {
    "preflight": {"passed"},
    "freshness": {"pending", "in_progress", "passed", "blocked", "failed", "review_required"},
    "blocker": {"clear", "recorded"},
    "repair": {
        "not_required",
        "proposal_in_progress",
        "proposed",
        "in_progress",
        "passed",
        "failed",
        "review_required",
    },
    "verification": {"pending", "in_progress", "passed", "failed", "review_required"},
    "deployment": {"unverified", "passed"},
    "payload": {"unverified", "passed"},
    "pre_send": {"unverified", "passed"},
    "receipt": {"unverified"},
}


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


_ACTIVE_AUTHORITY = threading.local()


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class _EvidenceAuthority:
    """FD-anchored authority for the locked repository and ignored evidence tree."""

    def __init__(self, root: Path):
        self.root = root
        self.root_fd = -1
        self.out_fd = -1
        self.root_stat: os.stat_result | None = None
        self.out_stat: os.stat_result | None = None
        self.observed_dirs: dict[str, tuple[int, int]] = {}
        self.observed_files: dict[str, tuple[int, int, int, int, int, int, int]] = {}

    def acquire(self) -> None:
        try:
            self.root_fd = os.open(self.root, _directory_open_flags())
            self.root_stat = os.fstat(self.root_fd)
            root_path_stat = os.lstat(self.root)
            if (
                not stat.S_ISDIR(self.root_stat.st_mode)
                or self.root_stat.st_uid != os.getuid()
                or not _same_inode(self.root_stat, root_path_stat)
            ):
                raise RunEvidenceError("repository root is not a stable current-user directory")
            fcntl.flock(self.root_fd, fcntl.LOCK_EX)
            self._verify_root()
            try:
                os.mkdir("out", 0o700, dir_fd=self.root_fd)
            except FileExistsError:
                pass
            try:
                self.out_fd = os.open("out", _directory_open_flags(), dir_fd=self.root_fd)
            except OSError as exc:
                raise RunEvidenceError("out must be a nonsymlink repository-local directory") from exc
            self.out_stat = os.fstat(self.out_fd)
            out_path_stat = os.stat("out", dir_fd=self.root_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(self.out_stat.st_mode)
                or self.out_stat.st_uid != os.getuid()
                or not _same_inode(self.out_stat, out_path_stat)
            ):
                raise RunEvidenceError("out is not the held repository-local directory")
        except Exception:
            self.close(verify=False)
            raise

    def _verify_root(self) -> None:
        assert self.root_stat is not None
        current = os.lstat(self.root)
        descriptor = os.fstat(self.root_fd)
        if not _same_inode(self.root_stat, descriptor) or not _same_inode(descriptor, current):
            raise RunEvidenceError("repository root identity changed during the lane transition")

    def _verify_out(self) -> None:
        assert self.out_stat is not None
        descriptor = os.fstat(self.out_fd)
        current = os.stat("out", dir_fd=self.root_fd, follow_symlinks=False)
        if not _same_inode(self.out_stat, descriptor) or not _same_inode(descriptor, current):
            raise RunEvidenceError("out directory identity changed during the lane transition")

    @staticmethod
    def _parts(relative: Path | str) -> tuple[str, ...]:
        path = Path(relative)
        parts = path.parts
        if len(parts) < 2 or parts[0] != "out" or any(
            part in {"", ".", ".."} or "/" in part for part in parts
        ):
            raise RunEvidenceError("evidence path is outside the canonical out directory")
        return tuple(parts)

    @contextmanager
    def _parent(self, relative: Path | str, *, create: bool):
        parts = self._parts(relative)
        descriptors = [os.dup(self.out_fd)]
        links: list[tuple[int, str, int, os.stat_result]] = []
        try:
            current_fd = descriptors[0]
            for index, part in enumerate(parts[1:-1], start=1):
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                try:
                    child_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
                except FileNotFoundError as exc:
                    raise RunEvidenceError(
                        f"evidence parent is missing: {'/'.join(parts[:-1])}"
                    ) from exc
                except OSError as exc:
                    raise RunEvidenceError("evidence parent must not be a symlink") from exc
                child_stat = os.fstat(child_fd)
                current_stat = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or child_stat.st_uid != os.getuid()
                    or not _same_inode(child_stat, current_stat)
                ):
                    os.close(child_fd)
                    raise RunEvidenceError("evidence parent identity is invalid")
                relative_dir = "/".join(parts[: index + 1])
                directory_identity = (child_stat.st_dev, child_stat.st_ino)
                expected_identity = self.observed_dirs.get(relative_dir)
                if expected_identity is not None and expected_identity != directory_identity:
                    os.close(child_fd)
                    raise RunEvidenceError(
                        f"evidence parent identity changed during transition: {relative_dir}"
                    )
                self.observed_dirs[relative_dir] = directory_identity
                descriptors.append(child_fd)
                links.append((current_fd, part, child_fd, child_stat))
                current_fd = child_fd
            yield current_fd, parts[-1]
            for parent_fd, part, child_fd, expected in links:
                current = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                descriptor = os.fstat(child_fd)
                if not _same_inode(expected, descriptor) or not _same_inode(descriptor, current):
                    raise RunEvidenceError("evidence parent identity changed during access")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _validate_file_stat(value: os.stat_result) -> None:
        if stat.S_ISLNK(value.st_mode):
            raise RunEvidenceError("evidence file must not be a symlink")
        if not stat.S_ISREG(value.st_mode):
            raise RunEvidenceError("evidence file must be a regular file")
        if value.st_nlink != 1 or value.st_uid != os.getuid():
            raise RunEvidenceError("evidence file must be a uniquely owned regular file")

    @staticmethod
    def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_mode, value.st_nlink

    @staticmethod
    def _snapshot(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def _remember(
        self,
        relative: Path | str,
        value: os.stat_result,
        *,
        allow_authorized_replace: bool = False,
    ) -> None:
        name = Path(relative).as_posix()
        identity = self._snapshot(value)
        previous = self.observed_files.get(name)
        if previous is not None and previous != identity and not allow_authorized_replace:
            raise RunEvidenceError(f"evidence file identity changed during transition: {name}")
        self.observed_files[name] = identity

    def ensure_parent(self, relative: Path | str) -> None:
        with self._parent(relative, create=True):
            pass

    def file_exists(self, relative: Path | str) -> bool:
        with self._parent(relative, create=False) as (parent_fd, leaf):
            try:
                value = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            self._validate_file_stat(value)
            self._remember(relative, value)
            return True

    def read_bytes(self, relative: Path | str) -> bytes:
        with self._parent(relative, create=False) as (parent_fd, leaf):
            try:
                descriptor = os.open(
                    leaf,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise RunEvidenceError(f"evidence file is unavailable: {relative}") from exc
            try:
                opened = os.fstat(descriptor)
                current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                self._validate_file_stat(opened)
                if not _same_inode(opened, current):
                    raise RunEvidenceError("evidence file identity changed before read")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                after = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                if self._snapshot(opened) != self._snapshot(after):
                    raise RunEvidenceError("evidence file identity changed during read")
                self._remember(relative, after)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RunEvidenceError("evidence write did not make progress")
            view = view[written:]

    def atomic_write(self, relative: Path | str, payload: bytes) -> None:
        with self._parent(relative, create=True) as (parent_fd, leaf):
            try:
                existing = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                self._validate_file_stat(existing)
                previous = self.observed_files.get(Path(relative).as_posix())
                if previous is not None and self._snapshot(existing) != previous:
                    raise RunEvidenceError(
                        f"evidence file identity changed before atomic write: {relative}"
                    )
            temporary = f".{leaf}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
            descriptor = -1
            replaced = False
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
                self._write_all(descriptor, payload)
                os.fsync(descriptor)
                written = os.fstat(descriptor)
                self._validate_file_stat(written)
                os.replace(
                    temporary,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                replaced = True
                current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                if not _same_inode(written, current) or self._identity(written) != self._identity(
                    current
                ):
                    raise RunEvidenceError("atomic evidence identity changed during publication")
                os.fsync(parent_fd)
                self._remember(relative, current, allow_authorized_replace=True)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if not replaced:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass

    def exclusive_write(self, relative: Path | str, payload: bytes) -> None:
        with self._parent(relative, create=True) as (parent_fd, leaf):
            try:
                descriptor = os.open(
                    leaf,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError as exc:
                raise RunEvidenceError(f"exclusive evidence path already exists: {leaf}") from exc
            try:
                self._write_all(descriptor, payload)
                os.fsync(descriptor)
                written = os.fstat(descriptor)
                self._validate_file_stat(written)
                current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                if not _same_inode(written, current) or self._identity(written) != self._identity(
                    current
                ):
                    raise RunEvidenceError("exclusive evidence identity changed during creation")
                os.fsync(parent_fd)
                self._remember(relative, current)
            finally:
                os.close(descriptor)

    def verify(self) -> None:
        self._verify_root()
        self._verify_out()
        for relative in sorted(self.observed_dirs, key=lambda value: (value.count("/"), value)):
            with self._parent(Path(relative) / ".identity-check", create=False):
                pass
        for relative, expected in list(self.observed_files.items()):
            with self._parent(relative, create=False) as (parent_fd, leaf):
                current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                self._validate_file_stat(current)
                if self._snapshot(current) != expected:
                    raise RunEvidenceError(
                        f"evidence file identity changed before transition release: {relative}"
                    )

    def close(self, *, verify: bool) -> None:
        error: Exception | None = None
        if verify and self.root_fd >= 0 and self.out_fd >= 0:
            try:
                self.verify()
            except Exception as exc:
                error = exc
        if self.out_fd >= 0:
            os.close(self.out_fd)
            self.out_fd = -1
        if self.root_fd >= 0:
            try:
                fcntl.flock(self.root_fd, fcntl.LOCK_UN)
            finally:
                os.close(self.root_fd)
                self.root_fd = -1
        if error is not None:
            raise error


def _active_authority(root: Path) -> _EvidenceAuthority:
    authority = getattr(_ACTIVE_AUTHORITY, "value", None)
    if not isinstance(authority, _EvidenceAuthority) or authority.root != root:
        raise RunEvidenceError("evidence access requires the held repository transition lock")
    return authority


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

    authority = _active_authority(root)
    if create_parent:
        authority.ensure_parent(Path(relative))
    exists = authority.file_exists(Path(relative))
    if require_file and not exists:
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


def _evidence_relative(path: Path) -> tuple[_EvidenceAuthority, Path]:
    authority = getattr(_ACTIVE_AUTHORITY, "value", None)
    if not isinstance(authority, _EvidenceAuthority):
        raise RunEvidenceError("evidence access requires the held transition authority")
    try:
        relative = path.relative_to(authority.root)
    except ValueError as exc:
        raise RunEvidenceError("evidence path belongs to a different repository") from exc
    _EvidenceAuthority._parts(relative)
    return authority, relative


def _read_evidence_bytes(path: Path) -> bytes:
    authority, relative = _evidence_relative(path)
    return authority.read_bytes(relative)


def _evidence_exists(path: Path) -> bool:
    authority, relative = _evidence_relative(path)
    return authority.file_exists(relative)


def _atomic_json(path: Path, value: dict) -> None:
    if value.get("contract_role") == CONTRACT_ROLE:
        _validate_state_shape(value)
    payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    authority, relative = _evidence_relative(path)
    authority.atomic_write(relative, payload)


def _exclusive_json(path: Path, value: dict, *, validate_state: bool = True) -> None:
    """Create evidence without replacing any pre-existing path."""
    if validate_state and value.get("contract_role") == CONTRACT_ROLE:
        _validate_state_shape(value)
    payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    authority, relative = _evidence_relative(path)
    authority.exclusive_write(relative, payload)


@contextmanager
def _lane_lock(root: Path):
    """Serialize a full transition on the stable pre-existing repository directory."""
    root = _exact_repo_root(root)
    authority = _EvidenceAuthority(root)
    authority.acquire()
    previous = getattr(_ACTIVE_AUTHORITY, "value", None)
    _ACTIVE_AUTHORITY.value = authority
    try:
        yield authority
    finally:
        try:
            authority.close(verify=True)
        finally:
            if previous is None:
                try:
                    delattr(_ACTIVE_AUTHORITY, "value")
                except AttributeError:
                    pass
            else:
                _ACTIVE_AUTHORITY.value = previous


def _serialized_transition(function):
    @functools.wraps(function)
    def wrapped(root: Path, *args, **kwargs):
        exact_root = _exact_repo_root(Path(root))
        with _lane_lock(exact_root):
            return function(exact_root, *args, **kwargs)

    return wrapped


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


def _require_evidence_fields(name: str, status: str, evidence: dict, required: set[str]) -> None:
    missing = sorted(required - set(evidence))
    if missing:
        raise RunEvidenceError(
            f"run state stage {name} status {status} is missing evidence fields: {missing}"
        )


def _validate_verification_attempt(attempt: dict, expected_number: int) -> None:
    base = {
        "attempt",
        "status",
        "command",
        "cwd",
        "makefile_sha256",
        "started_at_utc",
        "input_source_sha256",
        "after_repair",
    }
    if not isinstance(attempt, dict) or not base.issubset(attempt):
        raise RunEvidenceError("verification attempt evidence schema is invalid")
    if attempt["attempt"] != expected_number or attempt["status"] not in {
        "in_progress",
        "passed",
        "failed",
        "review_required",
    }:
        raise RunEvidenceError("verification attempt identity or status is invalid")
    if attempt["command"] != [MAKE, "verify-current"]:
        raise RunEvidenceError("verification attempt command is not fixed")
    if not isinstance(attempt["after_repair"], bool):
        raise RunEvidenceError("verification repair marker is invalid")
    if not DIGEST_RE.fullmatch(str(attempt["makefile_sha256"] or "")):
        raise RunEvidenceError("verification Makefile digest is invalid")
    if not DIGEST_RE.fullmatch(str(attempt["input_source_sha256"] or "")):
        raise RunEvidenceError("verification input source digest is invalid")
    parse_utc(attempt["started_at_utc"])
    terminal = {
        "finished_at_utc",
        "exit_code",
        "stdout_sha256",
        "stderr_sha256",
        "output_source_sha256",
    }
    if attempt["status"] in {"passed", "failed"}:
        _require_evidence_fields("verification", attempt["status"], attempt, terminal)
        parse_utc(attempt["finished_at_utc"])
        if not isinstance(attempt["exit_code"], int) or isinstance(attempt["exit_code"], bool):
            raise RunEvidenceError("verification exit code is invalid")
        if (attempt["status"] == "passed") != (attempt["exit_code"] == 0):
            raise RunEvidenceError("verification status and exit code are inconsistent")
        for field in ("stdout_sha256", "stderr_sha256", "output_source_sha256"):
            if not DIGEST_RE.fullmatch(str(attempt[field] or "")):
                raise RunEvidenceError(f"verification {field} is invalid")
    elif terminal & set(attempt):
        raise RunEvidenceError("unfinished verification attempt contains terminal evidence")
    allowed = base | terminal
    if attempt["status"] == "review_required":
        allowed.add("review_required_reason")
        if not str(attempt.get("review_required_reason") or ""):
            raise RunEvidenceError("review-required verification lacks a reason")
    if set(attempt) != allowed - (terminal if attempt["status"] not in {"passed", "failed"} else set()):
        raise RunEvidenceError("verification attempt contains unexpected evidence fields")


def _validate_stage_evidence(name: str, stage: dict) -> None:
    status = stage["status"]
    evidence = stage["evidence"]
    if status not in STAGE_STATUSES[name]:
        raise RunEvidenceError(f"run state stage {name} has illegal status {status}")
    if name == "preflight":
        required = {
            "branch",
            "start_sha",
            "tracked_worktree_clean",
            "owner",
            "origin",
            "input_identity",
        }
        _require_evidence_fields(name, status, evidence, required)
        if evidence["branch"] != "main" or evidence["tracked_worktree_clean"] is not True:
            raise RunEvidenceError("preflight evidence does not prove clean main")
        _validate_owner(evidence["owner"])
        _validate_origin(evidence["origin"], allow_unverified=False)
        if not COMMIT_RE.fullmatch(str(evidence["start_sha"] or "")):
            raise RunEvidenceError("preflight start SHA is invalid")
        _validate_start_input_identity(evidence["input_identity"])
    elif name == "freshness":
        if status == "pending" and evidence:
            raise RunEvidenceError("pending freshness evidence must be empty")
        if status != "pending":
            _require_evidence_fields(
                name,
                status,
                evidence,
                {
                    "command",
                    "cwd",
                    "script_sha256",
                    "started_at_utc",
                    "input_source_sha256",
                    "target_identity",
                    "outcome_transport",
                },
            )
            cwd = Path(str(evidence["cwd"] or ""))
            expected_command = [
                PYTHON,
                os.fspath(cwd / "scripts/refresh.py"),
                "--outcome-path",
                os.fspath(cwd / "out/runs" / stage["run_id"] / "refresh-outcome.json"),
                "--run-id",
                stage["run_id"],
                "--exclusive-outcome",
            ]
            if evidence["command"] != expected_command:
                raise RunEvidenceError("freshness command evidence is not the fixed exclusive command")
            if evidence["outcome_transport"] != "inherited_parent_directory_fd":
                raise RunEvidenceError("freshness outcome transport is not FD-anchored")
            parse_utc(evidence["started_at_utc"])
            base = {
                "command",
                "cwd",
                "script_sha256",
                "started_at_utc",
                "input_source_sha256",
                "target_identity",
                "outcome_transport",
            }
            command_terminal = {
                "finished_at_utc",
                "exit_code",
                "stdout_sha256",
                "stderr_sha256",
            }
            completed = {
                "outcome_file",
                "outcome_sha256",
                "outcome",
                "output_source_identity",
                "output_target_identity",
                "listing_count",
                "spec_count",
            }
            allowed_sets = {
                "in_progress": [base],
                "failed": [base | command_terminal],
                "passed": [base | command_terminal | completed],
                "blocked": [base | command_terminal | completed],
                "review_required": [
                    base | {"review_required_reason"},
                    base | command_terminal | {"review_required_reason"},
                ],
            }
            if set(evidence) not in allowed_sets[status]:
                raise RunEvidenceError("freshness evidence schema is not exact for its status")
            for field in ("script_sha256", "input_source_sha256"):
                if not DIGEST_RE.fullmatch(str(evidence[field] or "")):
                    raise RunEvidenceError(f"freshness {field} is invalid")
            for field in ("stdout_sha256", "stderr_sha256"):
                if field in evidence and not DIGEST_RE.fullmatch(str(evidence[field] or "")):
                    raise RunEvidenceError(f"freshness {field} is invalid")
            if "finished_at_utc" in evidence:
                parse_utc(evidence["finished_at_utc"])
            if status == "review_required" and not str(
                evidence.get("review_required_reason") or ""
            ):
                raise RunEvidenceError("review-required freshness lacks a reason")
            if status in {"failed", "passed", "blocked"}:
                if not isinstance(evidence["exit_code"], int) or isinstance(
                    evidence["exit_code"], bool
                ):
                    raise RunEvidenceError("freshness exit code is invalid")
                if status == "failed" and evidence["exit_code"] == 0:
                    raise RunEvidenceError("failed freshness has a successful exit code")
                if status in {"passed", "blocked"} and evidence["exit_code"] != 0:
                    raise RunEvidenceError("completed freshness has a failed exit code")
            if status in {"passed", "blocked"}:
                outcome = evidence["outcome"]
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
                    raise RunEvidenceError("freshness outcome evidence schema is invalid")
                if outcome["run_id"] != stage["run_id"]:
                    raise RunEvidenceError("freshness outcome belongs to a different run")
                if evidence["output_source_identity"].get("source_sha256") != stage[
                    "source_sha"
                ]:
                    raise RunEvidenceError("freshness output source identity is inconsistent")
            elif evidence["input_source_sha256"] != stage["source_sha"]:
                raise RunEvidenceError("freshness input source identity is inconsistent")
    elif name == "blocker":
        if status == "clear" and evidence:
            raise RunEvidenceError("clear blocker evidence must be empty")
        if status == "recorded":
            if set(evidence) != {"failure_stage", "reason_code", "detail"}:
                raise RunEvidenceError("recorded blocker evidence schema is invalid")
            for field in evidence:
                if not str(evidence[field] or ""):
                    raise RunEvidenceError("recorded blocker evidence contains an empty field")
    elif name == "repair":
        if status == "not_required":
            if evidence != {
                "repair_id": None,
                "action": None,
                "attempts_used": 0,
                "max_attempts": 1,
            }:
                raise RunEvidenceError("not-required repair evidence schema is invalid")
        else:
            _require_evidence_fields(
                name,
                status,
                evidence,
                {
                    "repair_id",
                    "action",
                    "target_path",
                    "tool_path",
                    "tool_sha256",
                    "max_attempts",
                    "attempts_used",
                },
            )
            if evidence["repair_id"] != REPAIR_ID or evidence["max_attempts"] != 1:
                raise RunEvidenceError("repair evidence exceeds the allowlisted repair")
            if status in {"in_progress", "passed", "failed", "review_required"} and evidence["attempts_used"] != 1:
                raise RunEvidenceError("repair attempt was not consumed before execution")
            if status in {"proposal_in_progress", "proposed"} and evidence["attempts_used"] != 0:
                raise RunEvidenceError("repair proposal consumed an attempt too early")
            proposal = {
                "repair_id",
                "action",
                "target_path",
                "target_sha256_before",
                "tool_path",
                "tool_sha256",
                "precheck_command",
                "precheck_started_at_utc",
                "max_attempts",
                "attempts_used",
            }
            proposal_terminal = {
                "precheck_finished_at_utc",
                "precheck_exit_code",
                "precheck_stdout_sha256",
                "precheck_stderr_sha256",
                "kept_count",
                "remove_count",
            }
            execution = {"repair_command", "repair_started_at_utc"}
            execution_terminal = {
                "repair_finished_at_utc",
                "repair_exit_code",
                "repair_stdout_sha256",
                "repair_stderr_sha256",
                "target_sha256_after",
                "postcheck_command",
                "postcheck_exit_code",
                "postcheck_stdout_sha256",
                "postcheck_stderr_sha256",
            }
            expected_sets = {
                "proposal_in_progress": [proposal],
                "proposed": [proposal | proposal_terminal],
                "in_progress": [proposal | proposal_terminal | execution],
                "passed": [proposal | proposal_terminal | execution | execution_terminal],
                "failed": [proposal | proposal_terminal | execution | execution_terminal],
                "review_required": [
                    proposal | {"review_required_reason"},
                    proposal | proposal_terminal | execution | {"review_required_reason"},
                ],
            }
            if set(evidence) not in expected_sets[status]:
                raise RunEvidenceError("repair evidence schema is not exact for its status")
            if evidence["action"] != "remove_only_estimated_history_rows":
                raise RunEvidenceError("repair evidence names an unapproved action")
            if evidence["target_path"] != "history.csv" or evidence["tool_path"] != "scripts/repair_history.py":
                raise RunEvidenceError("repair evidence names an unapproved path")
            for field in ("target_sha256_before", "tool_sha256"):
                if not DIGEST_RE.fullmatch(str(evidence[field] or "")):
                    raise RunEvidenceError(f"repair {field} is invalid")
            precheck_command = evidence.get("precheck_command")
            if not isinstance(precheck_command, list) or len(precheck_command) != 5:
                raise RunEvidenceError("repair precheck command schema is invalid")
            target = str(Path(precheck_command[3]))
            expected_precheck = [
                PYTHON,
                str(Path(precheck_command[1])),
                "--path",
                target,
                "--check",
            ]
            if precheck_command != expected_precheck:
                raise RunEvidenceError("repair precheck command is not fixed")
            if status in {"in_progress", "passed", "failed"}:
                expected_repair = expected_precheck[:-1]
                if evidence["repair_command"] != expected_repair:
                    raise RunEvidenceError("repair execution command is not fixed")
            if status in {"passed", "failed"} and evidence["postcheck_command"] != expected_precheck:
                raise RunEvidenceError("repair postcheck command is not fixed")
            if status == "review_required" and not str(
                evidence.get("review_required_reason") or ""
            ):
                raise RunEvidenceError("review-required repair lacks a reason")
            parse_utc(evidence["precheck_started_at_utc"])
            if status in {"proposed", "in_progress", "passed", "failed"}:
                parse_utc(evidence["precheck_finished_at_utc"])
                for field in ("precheck_exit_code", "kept_count", "remove_count"):
                    if not isinstance(evidence[field], int) or isinstance(evidence[field], bool):
                        raise RunEvidenceError(f"repair {field} is invalid")
                for field in ("precheck_stdout_sha256", "precheck_stderr_sha256"):
                    if not DIGEST_RE.fullmatch(str(evidence[field] or "")):
                        raise RunEvidenceError(f"repair {field} is invalid")
            if status in {"in_progress", "passed", "failed"}:
                parse_utc(evidence["repair_started_at_utc"])
            if status in {"passed", "failed"}:
                parse_utc(evidence["repair_finished_at_utc"])
                for field in ("repair_exit_code", "postcheck_exit_code"):
                    if not isinstance(evidence[field], int) or isinstance(evidence[field], bool):
                        raise RunEvidenceError(f"repair {field} is invalid")
                for field in (
                    "repair_stdout_sha256",
                    "repair_stderr_sha256",
                    "target_sha256_after",
                    "postcheck_stdout_sha256",
                    "postcheck_stderr_sha256",
                ):
                    if not DIGEST_RE.fullmatch(str(evidence[field] or "")):
                        raise RunEvidenceError(f"repair {field} is invalid")
                passed = evidence["repair_exit_code"] == 0 and evidence["postcheck_exit_code"] == 0
                if (status == "passed") != passed:
                    raise RunEvidenceError("repair status and exit evidence are inconsistent")
    elif name == "verification":
        if set(evidence) != {"attempts"} or not isinstance(evidence["attempts"], list):
            raise RunEvidenceError("verification evidence schema is invalid")
        if len(evidence["attempts"]) > 2:
            raise RunEvidenceError("verification attempt budget is exceeded")
        for number, attempt in enumerate(evidence["attempts"], start=1):
            _validate_verification_attempt(attempt, number)
        if status == "pending" and evidence["attempts"] and evidence["attempts"][-1]["status"] != "failed":
            raise RunEvidenceError("pending verification lacks a prior failed attempt")
        if status in {"in_progress", "passed", "failed", "review_required"}:
            if not evidence["attempts"] or evidence["attempts"][-1]["status"] != status:
                raise RunEvidenceError("verification stage and latest attempt statuses differ")
            if evidence["attempts"][-1]["input_source_sha256"] != stage["source_sha"]:
                raise RunEvidenceError("verification source identity is inconsistent")
    elif name == "deployment":
        if status == "unverified" and evidence:
            raise RunEvidenceError("unverified deployment evidence must be empty")
        if status == "passed":
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
            if set(evidence) != required:
                raise RunEvidenceError("passed deployment evidence schema is not exact")
            compact_origin = evidence["origin"]
            if not isinstance(compact_origin, dict) or set(compact_origin) != {
                "repository",
                "fetch_url",
                "push_url",
                "live_main_sha",
            }:
                raise RunEvidenceError("deployment origin evidence schema is invalid")
            if compact_origin["repository"] != EXPECTED_ORIGIN_REPOSITORY:
                raise RunEvidenceError("deployment evidence names the wrong repository")
            if compact_origin["fetch_url"] not in ALLOWED_ORIGIN_URLS or compact_origin[
                "push_url"
            ] not in ALLOWED_ORIGIN_URLS:
                raise RunEvidenceError("deployment evidence contains an unapproved origin")
            if evidence["result_sha"] != evidence["origin_main_sha"] or evidence[
                "result_sha"
            ] != compact_origin["live_main_sha"]:
                raise RunEvidenceError("deployment evidence SHAs are inconsistent")
            if evidence["source_sha"] != stage["source_sha"]:
                raise RunEvidenceError("deployment source identity is inconsistent")
            if evidence["public_url"] != CANONICAL_DASHBOARD_URL:
                raise RunEvidenceError("deployment evidence names a non-canonical public URL")
            for field in ("source_sha", "bundle_marker_sha256"):
                if not DIGEST_RE.fullmatch(str(evidence[field] or "")):
                    raise RunEvidenceError(f"deployment {field} is invalid")
            if not isinstance(evidence["files"], dict) or not evidence["files"]:
                raise RunEvidenceError("deployment file evidence is invalid")
            if not isinstance(evidence["fetches"], dict) or set(evidence["fetches"]) != set(
                evidence["files"]
            ):
                raise RunEvidenceError("deployment fetch evidence is incomplete")
    elif name == "payload":
        if status == "unverified" and evidence:
            raise RunEvidenceError("unverified payload evidence must be empty")
        if status == "passed":
            if set(evidence) != {
                "payload_sha256",
                "payload_file",
                "generated_at_utc",
                "to",
                "cc",
                "bcc",
                "subject",
                "source_identity",
                "origin",
            }:
                raise RunEvidenceError("passed payload evidence schema is invalid")
            _validate_origin(evidence["origin"], allow_unverified=False)
            parse_utc(evidence["generated_at_utc"])
            if (
                evidence["payload_file"] != "out/latest-email.json"
                or not DIGEST_RE.fullmatch(str(evidence["payload_sha256"] or ""))
                or evidence["to"] != list(EXPECTED_RECIPIENTS)
                or evidence["cc"] != []
                or evidence["bcc"] != []
            ):
                raise RunEvidenceError("payload evidence violates its exact delivery boundary")
            if evidence["origin"]["observed_at_utc"] != stage["observed_at_utc"]:
                raise RunEvidenceError("payload origin timestamp is inconsistent")
            if not isinstance(evidence["source_identity"], dict) or evidence[
                "source_identity"
            ].get("source_sha256") != stage["source_sha"]:
                raise RunEvidenceError("payload source identity is inconsistent")
    elif name == "pre_send":
        if status == "unverified" and evidence:
            raise RunEvidenceError("unverified pre-send evidence must be empty")
        if status == "passed":
            if set(evidence) != {
                "payload_sha256",
                "payload_file",
                "validated_at_utc",
                "generated_at_utc",
                "to",
                "cc",
                "bcc",
                "subject",
                "source_identity",
                "origin",
            }:
                raise RunEvidenceError("passed pre-send evidence schema is invalid")
            _validate_origin(evidence["origin"], allow_unverified=False)
            if evidence["validated_at_utc"] != stage["observed_at_utc"]:
                raise RunEvidenceError("pre-send validation timestamps are inconsistent")
            parse_utc(evidence["generated_at_utc"])
            if (
                evidence["payload_file"] != "out/latest-email.json"
                or not DIGEST_RE.fullmatch(str(evidence["payload_sha256"] or ""))
                or evidence["to"] != list(EXPECTED_RECIPIENTS)
                or evidence["cc"] != []
                or evidence["bcc"] != []
            ):
                raise RunEvidenceError("pre-send evidence violates its exact delivery boundary")
            if evidence["origin"]["observed_at_utc"] != stage["observed_at_utc"]:
                raise RunEvidenceError("pre-send origin timestamp is inconsistent")
            if not isinstance(evidence["source_identity"], dict) or evidence[
                "source_identity"
            ].get("source_sha256") != stage["source_sha"]:
                raise RunEvidenceError("pre-send source identity is inconsistent")
    elif name == "receipt":
        if evidence != {"reason_code": "trusted_receipt_adapter_unavailable"}:
            raise RunEvidenceError("receipt evidence must remain explicitly unverified")


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
    if state["status"] == "running" and state["origin_at_finish"] is not None:
        raise RunEvidenceError("running state cannot contain final origin evidence")
    if state["status"] != "running" and state["origin_at_finish"] is None:
        raise RunEvidenceError("terminal state requires final origin evidence")
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
        if parse_utc(stage["observed_at_utc"]) < parse_utc(state["started_at_utc"]):
            raise RunEvidenceError(f"run state stage {name} predates the run")
        _validate_stage_evidence(name, stage)
    repo_root = Path(str(state["repo_root"] or ""))
    if not repo_root.is_absolute():
        raise RunEvidenceError("run state repository root must be absolute")
    repo_text = os.fspath(repo_root)
    preflight_evidence = state["stages"]["preflight"]["evidence"]
    if (
        preflight_evidence["start_sha"] != state["start_sha"]
        or preflight_evidence["owner"] != state["owner"]
        or preflight_evidence["origin"] != state["origin_at_start"]
    ):
        raise RunEvidenceError("preflight evidence differs from the run identity")
    freshness_evidence = state["stages"]["freshness"]["evidence"]
    if state["stages"]["freshness"]["status"] != "pending":
        if freshness_evidence["cwd"] != repo_text or freshness_evidence["command"][1] != os.fspath(
            repo_root / "scripts/refresh.py"
        ):
            raise RunEvidenceError("freshness evidence names a different repository root")
    for attempt in state["stages"]["verification"]["evidence"]["attempts"]:
        if attempt["cwd"] != repo_text:
            raise RunEvidenceError("verification evidence names a different repository root")
    repair_evidence = state["stages"]["repair"]["evidence"]
    if state["stages"]["repair"]["status"] != "not_required":
        expected_precheck = [
            PYTHON,
            os.fspath(repo_root / "scripts/repair_history.py"),
            "--path",
            os.fspath(repo_root / "history.csv"),
            "--check",
        ]
        if repair_evidence["precheck_command"] != expected_precheck:
            raise RunEvidenceError("repair evidence names a different repository root")
        if "repair_command" in repair_evidence and repair_evidence["repair_command"] != expected_precheck[
            :-1
        ]:
            raise RunEvidenceError("repair execution evidence names a different repository root")
        if "postcheck_command" in repair_evidence and repair_evidence[
            "postcheck_command"
        ] != expected_precheck:
            raise RunEvidenceError("repair postcheck evidence names a different repository root")
    if state["recovery"] is not None:
        recovery = state["recovery"]
        if not isinstance(recovery, dict) or set(recovery) != {
            "status",
            "observed_at_utc",
            "expected_run_id",
            "minimum_age_seconds",
            "age_seconds",
            "owner",
            "expected_origin_sha",
            "origin",
        }:
            raise RunEvidenceError("run recovery evidence schema is invalid")
        if recovery["status"] != "stale_owner_recovered":
            raise RunEvidenceError("run recovery status is invalid")
        if recovery["expected_run_id"] != state["run_id"] or recovery["owner"] != state["owner"]:
            raise RunEvidenceError("run recovery identity differs from the occupied run")
        if recovery["minimum_age_seconds"] != int(STALE_RUN_AGE.total_seconds()) or not isinstance(
            recovery["age_seconds"], int
        ) or recovery["age_seconds"] < recovery["minimum_age_seconds"]:
            raise RunEvidenceError("run recovery age evidence is invalid")
        expected_sha = state["result_sha"] or state["start_sha"]
        if recovery["expected_origin_sha"] != expected_sha:
            raise RunEvidenceError("run recovery expected SHA is inconsistent")
        _validate_origin(recovery["origin"], allow_unverified=False)
        if recovery["origin"]["live_main_sha"] != expected_sha:
            raise RunEvidenceError("run recovery origin SHA is inconsistent")
        parse_utc(recovery["observed_at_utc"])
        if state["status"] != "failed":
            raise RunEvidenceError("recovered run must have a failed terminal outcome")
        if (
            recovery["observed_at_utc"] != state["finished_at_utc"]
            or recovery["origin"] != state["origin_at_finish"]
        ):
            raise RunEvidenceError("run recovery evidence differs from the terminal record")
    if state["finished_at_utc"] is not None and parse_utc(state["finished_at_utc"]) < parse_utc(
        state["started_at_utc"]
    ):
        raise RunEvidenceError("run finish predates start")
    if state["result_sha"] is not None:
        preflight = state["stages"]["preflight"]["evidence"]
        if set(preflight) != {
            "branch",
            "start_sha",
            "tracked_worktree_clean",
            "owner",
            "origin",
            "input_identity",
            "result_bound_at_utc",
            "result_origin",
            "changed_paths",
        }:
            raise RunEvidenceError("bound result preflight evidence schema is invalid")
        if state["stages"]["verification"]["status"] != "passed":
            raise RunEvidenceError("bound result requires passed verification")
        _validate_origin(preflight["result_origin"], allow_unverified=False)
        if preflight["result_origin"]["live_main_sha"] != state["result_sha"]:
            raise RunEvidenceError("result origin evidence differs from the bound result")
        if not isinstance(preflight["changed_paths"], list) or preflight["changed_paths"] != sorted(
            set(preflight["changed_paths"])
        ) or set(preflight["changed_paths"]) - ALLOWED_RESULT_PATHS:
            raise RunEvidenceError("bound result changed-path evidence is invalid")
    else:
        if set(state["stages"]["preflight"]["evidence"]) != {
            "branch",
            "start_sha",
            "tracked_worktree_clean",
            "owner",
            "origin",
            "input_identity",
        }:
            raise RunEvidenceError("unbound result preflight evidence schema is invalid")
    if state["stages"]["payload"]["status"] == "passed" and state["stages"]["deployment"]["status"] != "passed":
        raise RunEvidenceError("passed payload requires passed deployment")
    if state["stages"]["pre_send"]["status"] == "passed" and state["stages"]["payload"]["status"] != "passed":
        raise RunEvidenceError("passed pre-send requires passed payload")
    if state["status"] in {"blocked", "failed"} and state["stages"]["blocker"]["status"] != "recorded":
        raise RunEvidenceError("blocked and failed terminal outcomes require blocker evidence")
    in_progress = [
        name
        for name in ("freshness", "repair", "verification")
        if state["stages"][name]["status"] in {"in_progress", "proposal_in_progress"}
    ]
    if len(in_progress) > 1:
        raise RunEvidenceError("more than one owned command is marked in progress")
    if state["status"] != "running" and in_progress:
        raise RunEvidenceError("terminal run cannot retain an in-progress command")
    if state["stages"]["deployment"]["status"] == "passed":
        if state["result_sha"] is None or state["stages"]["freshness"]["status"] != "passed":
            raise RunEvidenceError("passed deployment requires a fresh bound result")
        if state["stages"]["deployment"]["evidence"]["result_sha"] != state["result_sha"]:
            raise RunEvidenceError("deployment evidence differs from the bound result")
    if state["stages"]["pre_send"]["status"] == "passed":
        if state["stages"]["pre_send"]["evidence"]["payload_sha256"] != state["stages"][
            "payload"
        ]["evidence"]["payload_sha256"]:
            raise RunEvidenceError("pre-send evidence differs from the payload binding")
    if state["status"] == "blocked" and state["result_sha"] is None:
        raise RunEvidenceError("blocked terminal outcome requires a bound result")
    if state["status"] == "delivery_unverified":
        if state["result_sha"] is None:
            raise RunEvidenceError("delivery-unverified outcome requires a bound result")
        for required_stage in ("freshness", "verification", "deployment", "payload", "pre_send"):
            if state["stages"][required_stage]["status"] != "passed":
                raise RunEvidenceError(
                    f"delivery-unverified outcome requires {required_stage}=passed"
                )
        if state["stages"]["receipt"]["status"] != "unverified":
            raise RunEvidenceError("delivery-unverified outcome cannot claim a receipt")


def _load_state(root: Path, state_path: Path | str) -> tuple[Path, dict]:
    path = _state_path(root, state_path)
    try:
        state = json.loads(_read_evidence_bytes(path).decode("utf-8"))
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


def _tracked_manifest(root: Path, commit: str) -> list[dict]:
    result = _git(root, "ls-tree", "-r", "-z", commit, text=False)
    entries: list[dict] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = header.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise RunEvidenceError("tracked Git manifest is malformed") from exc
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise RunEvidenceError(f"unsupported tracked object in run manifest: {path}")
        raw = _git(root, "cat-file", "blob", object_id, text=False).stdout
        entries.append(
            {
                "path": path,
                "mode": mode,
                "blob_id": object_id,
                "sha256": _sha256(raw),
            }
        )
    entries.sort(key=lambda entry: entry["path"])
    return entries


def _manifest_digest(entries: list[dict]) -> str:
    return _sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _manifest_by_path(entries: list[dict]) -> dict[str, dict]:
    return {entry["path"]: entry for entry in entries}


def _build_start_input_identity(
    root: Path,
    start_sha: str,
    listings: list[dict],
    source_identity: dict,
) -> dict:
    manifest = _tracked_manifest(root, start_sha)
    by_path = _manifest_by_path(manifest)
    missing = sorted(FIXED_COMMAND_PATHS - set(by_path))
    if missing:
        raise RunEvidenceError(f"fixed command inputs are not tracked: {missing}")
    return {
        "schema_version": 1,
        "start_tree_sha": _git_text(root, "rev-parse", f"{start_sha}^{{tree}}"),
        "tracked_manifest_sha256": _manifest_digest(manifest),
        "tracked_manifest": manifest,
        "allowed_result_paths": sorted(ALLOWED_RESULT_PATHS),
        "fixed_command_blobs": {
            path: by_path[path] for path in sorted(FIXED_COMMAND_PATHS)
        },
        "payload_source_identity": source_identity,
        "refresh_target_identity": build_refresh_target_identity(listings),
    }


def _validate_manifest_entry(entry: dict) -> None:
    if not isinstance(entry, dict) or set(entry) != {"path", "mode", "blob_id", "sha256"}:
        raise RunEvidenceError("Git manifest entry schema is invalid")
    path = str(entry["path"] or "")
    if not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise RunEvidenceError("Git manifest path is invalid")
    if entry["mode"] not in {"100644", "100755"}:
        raise RunEvidenceError("Git manifest mode is unsupported")
    if not COMMIT_RE.fullmatch(str(entry["blob_id"] or "")):
        raise RunEvidenceError("Git manifest blob identity is invalid")
    if not DIGEST_RE.fullmatch(str(entry["sha256"] or "")):
        raise RunEvidenceError("Git manifest content digest is invalid")


def _validate_start_input_identity(identity: dict) -> None:
    required = {
        "schema_version",
        "start_tree_sha",
        "tracked_manifest_sha256",
        "tracked_manifest",
        "allowed_result_paths",
        "fixed_command_blobs",
        "payload_source_identity",
        "refresh_target_identity",
    }
    if not isinstance(identity, dict) or set(identity) != required or identity["schema_version"] != 1:
        raise RunEvidenceError("start input identity schema is invalid")
    if not COMMIT_RE.fullmatch(str(identity["start_tree_sha"] or "")):
        raise RunEvidenceError("start tree identity is invalid")
    manifest = identity["tracked_manifest"]
    if not isinstance(manifest, list) or not manifest:
        raise RunEvidenceError("start tracked manifest is empty or invalid")
    for entry in manifest:
        _validate_manifest_entry(entry)
    paths = [entry["path"] for entry in manifest]
    if paths != sorted(set(paths)):
        raise RunEvidenceError("start tracked manifest paths are not unique and ordered")
    if identity["tracked_manifest_sha256"] != _manifest_digest(manifest):
        raise RunEvidenceError("start tracked manifest digest is invalid")
    if identity["allowed_result_paths"] != sorted(ALLOWED_RESULT_PATHS):
        raise RunEvidenceError("result changed-path allowlist is invalid")
    fixed = identity["fixed_command_blobs"]
    if not isinstance(fixed, dict) or set(fixed) != FIXED_COMMAND_PATHS:
        raise RunEvidenceError("fixed command blob manifest is incomplete")
    by_path = _manifest_by_path(manifest)
    for path, entry in fixed.items():
        if entry != by_path.get(path):
            raise RunEvidenceError(f"fixed command blob is not bound to the start tree: {path}")
    if not isinstance(identity["payload_source_identity"], dict):
        raise RunEvidenceError("start payload source identity is invalid")
    target = identity["refresh_target_identity"]
    if (
        not isinstance(target, dict)
        or set(target) != {"schema_version", "source_count", "target_manifest_sha256"}
        or target.get("schema_version") != 1
        or not isinstance(target.get("source_count"), int)
        or target["source_count"] <= 0
        or not DIGEST_RE.fullmatch(str(target.get("target_manifest_sha256") or ""))
    ):
        raise RunEvidenceError("start refresh target identity is invalid")


def _status_is_clean(root: Path) -> bool:
    return not bool(_git_text(root, "status", "--porcelain", "--untracked-files=all"))


def _assert_working_entry(root: Path, entry: dict) -> None:
    path = root / entry["path"]
    _assert_plain_path(path)
    if not path.is_file() or _sha256(path.read_bytes()) != entry["sha256"]:
        raise RunEvidenceError(f"working file differs from its bound Git blob: {entry['path']}")


def _assert_full_start_inputs_current(root: Path, state: dict) -> None:
    identity = state["stages"]["preflight"]["evidence"]["input_identity"]
    if _git_text(root, "rev-parse", "HEAD") != state["start_sha"]:
        raise RunEvidenceError("HEAD moved before the run-bound refresh")
    if not _status_is_clean(root):
        raise RunEvidenceError("start inputs have tracked or untracked drift")
    current = _tracked_manifest(root, state["start_sha"])
    if current != identity["tracked_manifest"]:
        raise RunEvidenceError("start Git manifest drifted before command execution")
    listings, _, _, source_identity = _load_sources(root)
    if source_identity != identity["payload_source_identity"]:
        raise RunEvidenceError("full start payload inputs drifted before refresh")
    if build_refresh_target_identity(listings) != identity["refresh_target_identity"]:
        raise RunEvidenceError("full start target inventory drifted before refresh")
    for entry in current:
        _assert_working_entry(root, entry)


def _assert_immutable_inputs_current(root: Path, state: dict) -> None:
    identity = state["stages"]["preflight"]["evidence"]["input_identity"]
    start_by_path = _manifest_by_path(identity["tracked_manifest"])
    immutable = {
        path: entry
        for path, entry in start_by_path.items()
        if path not in ALLOWED_RESULT_PATHS
    }
    current_head = _git_text(root, "rev-parse", "HEAD")
    current_by_path = _manifest_by_path(_tracked_manifest(root, current_head))
    current_immutable = {
        path: entry
        for path, entry in current_by_path.items()
        if path not in ALLOWED_RESULT_PATHS
    }
    if current_immutable != immutable:
        raise RunEvidenceError("immutable run Git blobs changed after start")
    unexpected = _git_text(root, "ls-files", "--others", "--exclude-standard")
    if unexpected:
        raise RunEvidenceError("untracked files appeared outside ignored run evidence")
    for entry in immutable.values():
        _assert_working_entry(root, entry)


def _assert_result_provenance(root: Path, state: dict) -> None:
    result_sha = state.get("result_sha")
    if not result_sha:
        raise RunEvidenceError("result provenance requires a bound result_sha")
    ancestor = _git(
        root,
        "merge-base",
        "--is-ancestor",
        state["start_sha"],
        result_sha,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RunEvidenceError("run start is not an ancestor of the result commit")
    changed_raw = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        state["start_sha"],
        result_sha,
        text=False,
    ).stdout
    try:
        changed = {value.decode("utf-8") for value in changed_raw.split(b"\0") if value}
    except UnicodeDecodeError as exc:
        raise RunEvidenceError("result changed-path evidence is not UTF-8") from exc
    unexpected = sorted(changed - ALLOWED_RESULT_PATHS)
    if unexpected:
        raise RunEvidenceError(f"result changed paths exceed the allowlist: {unexpected}")
    start_identity = state["stages"]["preflight"]["evidence"]["input_identity"]
    start_by_path = _manifest_by_path(start_identity["tracked_manifest"])
    result_by_path = _manifest_by_path(_tracked_manifest(root, result_sha))
    for path, start_entry in start_by_path.items():
        if path not in ALLOWED_RESULT_PATHS and result_by_path.get(path) != start_entry:
            raise RunEvidenceError(f"result commit changed an immutable Git blob: {path}")
    result_extra = set(result_by_path) - set(start_by_path)
    if result_extra:
        raise RunEvidenceError(f"result commit added unexpected tracked paths: {sorted(result_extra)}")


def _assert_clean_bound_result(
    root: Path,
    state: dict,
    observed_at: datetime,
    origin_reader: "OriginReader",
) -> dict:
    if _git_text(root, "branch", "--show-current") != "main":
        raise RunEvidenceError("delivery checks require branch main")
    if _git_text(root, "rev-parse", "HEAD") != state.get("result_sha"):
        raise RunEvidenceError("local HEAD no longer matches the bound result")
    if not _status_is_clean(root):
        raise RunEvidenceError("delivery checks require a clean tracked and untracked worktree")
    _assert_result_provenance(root, state)
    _assert_immutable_inputs_current(root, state)
    origin = origin_reader(root, observed_at)
    _validate_origin(origin, allow_unverified=False)
    _same_origin_repository(state["origin_at_start"], origin)
    if origin["live_main_sha"] != state["result_sha"]:
        raise RunEvidenceError("live origin/main no longer matches the bound result")
    return origin


def _assert_result_source_blobs(root: Path, state: dict) -> None:
    """Require every payload source byte to come from the bound result commit."""
    result_sha = state.get("result_sha")
    if not result_sha:
        raise RunEvidenceError("payload source validation requires a bound result")
    for relative in ("data/listings.json", "data/specs.json", "data/refresh-status.json"):
        committed = _git(root, "show", f"{result_sha}:{relative}", text=False).stdout
        current = (root / relative).read_bytes()
        if current != committed:
            raise RunEvidenceError(
                f"payload source is not the exact bound result Git blob: {relative}"
            )


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


def _archive_terminal_state(
    root: Path,
    state_path: Path,
    state: dict,
    *,
    validate_state: bool = True,
) -> None:
    raw = _read_evidence_bytes(state_path)
    compact_time = re.sub(r"[^0-9]", "", state["started_at_utc"])
    name = f"{state['run_id']}-{compact_time}-{_sha256(raw)[:12]}.json"
    relative = Path("out/run-state-archive") / name
    destination = _canonical_out_path(
        root,
        root / relative,
        relative,
        create_parent=True,
    )
    if _evidence_exists(destination):
        if _read_evidence_bytes(destination) != raw:
            raise RunEvidenceError("terminal run archive collision requires review")
        return
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError("terminal run cannot be archived safely") from exc
    _exclusive_json(destination, value, validate_state=validate_state)


def _is_archivable_legacy_terminal(root: Path, state: object) -> bool:
    """Recognize only prior terminal schemas; never retire an old running lane."""
    if not isinstance(state, dict):
        return False
    if state.get("contract_role") != CONTRACT_ROLE or state.get("schema_version") not in {1, 2, 3}:
        return False
    if state.get("status") not in {"blocked", "failed", "delivery_unverified"}:
        return False
    if state.get("finished_at_utc") is None:
        return False
    try:
        _validate_id(state.get("run_id"), "legacy run_id")
        parse_utc(state.get("started_at_utc"))
        parse_utc(state.get("finished_at_utc"))
        if Path(str(state.get("repo_root") or "")).resolve() != root:
            return False
        if not COMMIT_RE.fullmatch(str(state.get("start_sha") or "")):
            return False
        if state.get("result_sha") is not None and not COMMIT_RE.fullmatch(
            str(state.get("result_sha"))
        ):
            return False
    except (OSError, RunEvidenceError, TypeError, ValueError):
        return False
    return True


@_serialized_transition
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
    occupied = _evidence_exists(path)
    if occupied:
        try:
            previous = json.loads(_read_evidence_bytes(path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunEvidenceError("existing run state is invalid; preserve it for review") from exc
        try:
            _validate_state_shape(previous)
        except RunEvidenceError as exc:
            if not _is_archivable_legacy_terminal(root, previous):
                raise RunEvidenceError(
                    "existing run state is invalid or unfinished; preserve it for review"
                ) from exc
            _archive_terminal_state(root, path, previous, validate_state=False)
        else:
            if previous["status"] == "running":
                raise RunEvidenceError("an unfinished run state already occupies this lane")
            _archive_terminal_state(root, path, previous)

    owner_pid = int(owner_pid or os.getpid())
    owner_token = _pid_start_token(owner_pid)
    if owner_pid <= 1 or owner_token is None:
        raise RunEvidenceError("run owner process is not currently live")
    if _git_text(root, "branch", "--show-current") != "main":
        raise RunEvidenceError("automation run must start on main")
    if not _status_is_clean(root):
        raise RunEvidenceError("tracked and untracked worktree changes must be resolved before start")
    start_sha = _git_text(root, "rev-parse", "HEAD")
    origin = origin_reader(root, observed_at)
    _validate_origin(origin, allow_unverified=False)
    if origin["live_main_sha"] != start_sha:
        raise RunEvidenceError("local main must match live origin/main before the run starts")
    listings, _, _, start_identity = _load_sources(root)
    listing_source_urls(listings)
    for relative in sorted(FIXED_COMMAND_PATHS | {"history.csv"}):
        fixed_path = root / relative
        _assert_plain_path(fixed_path)
        if not fixed_path.is_file():
            raise RunEvidenceError(f"fixed run dependency must be a regular file: {relative}")
        if _git(root, "ls-files", "--error-unmatch", "--", relative, check=False).returncode != 0:
            raise RunEvidenceError(f"fixed run dependency must be tracked by git: {relative}")
    input_identity = _build_start_input_identity(root, start_sha, listings, start_identity)
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
                    "input_identity": input_identity,
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
            "pre_send": _stage("unverified", observed_at, run_id, None),
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
    if occupied:
        _atomic_json(path, state)
    else:
        _exclusive_json(path, state)
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
    pass_fds: tuple[int, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("KEG_TRACKER_OFFLINE", None)
    env.pop("KEG_EVIDENCE_DIR_FD", None)
    if extra_env:
        env.update(extra_env)
    result = runner(
        command,
        cwd=os.fspath(root),
        env=env,
        capture_output=True,
        check=False,
        pass_fds=pass_fds,
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


def _mark_interrupted_attempts_review_required(
    state: dict,
    observed_at: datetime,
    reason: str,
) -> None:
    freshness = state["stages"]["freshness"]
    if freshness["status"] == "in_progress":
        evidence = {**freshness["evidence"], "review_required_reason": reason}
        state["stages"]["freshness"] = _stage(
            "review_required",
            observed_at,
            state["run_id"],
            freshness["source_sha"],
            evidence,
        )
    repair = state["stages"]["repair"]
    if repair["status"] in {"proposal_in_progress", "in_progress"}:
        evidence = {
            **repair["evidence"],
            "attempts_used": 1,
            "review_required_reason": reason,
        }
        state["stages"]["repair"] = _stage(
            "review_required",
            observed_at,
            state["run_id"],
            repair["source_sha"],
            evidence,
        )
    verification = state["stages"]["verification"]
    if verification["status"] == "in_progress":
        attempts = list(verification["evidence"]["attempts"])
        latest = dict(attempts[-1])
        latest["status"] = "review_required"
        latest["review_required_reason"] = reason
        attempts[-1] = latest
        state["stages"]["verification"] = _stage(
            "review_required",
            observed_at,
            state["run_id"],
            verification["source_sha"],
            {"attempts": attempts},
        )


@_serialized_transition
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
    _assert_full_start_inputs_current(root, state)
    started = parse_utc(now or _now())
    assert started is not None
    listings_before, _, _, identity_before = _load_sources(root)
    target_before = build_refresh_target_identity(listings_before)
    expected_target = state["stages"]["preflight"]["evidence"]["input_identity"][
        "refresh_target_identity"
    ]
    if target_before != expected_target:
        raise RunEvidenceError("refresh targets drifted after run preflight")
    relative = Path("out/runs") / state["run_id"] / "refresh-outcome.json"
    outcome_path = _canonical_out_path(
        root,
        root / relative,
        relative,
        create_parent=True,
    )
    if _evidence_exists(outcome_path):
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
        "--exclusive-outcome",
    ]
    invocation = {
        "command": command,
        "cwd": os.fspath(root),
        "script_sha256": _sha256(script.read_bytes()),
        "started_at_utc": utc_iso(started),
        "input_source_sha256": identity_before["source_sha256"],
        "target_identity": target_before,
        "outcome_transport": "inherited_parent_directory_fd",
    }
    state["stages"]["freshness"] = _stage(
        "in_progress", started, state["run_id"], identity_before["source_sha256"], invocation
    )
    _atomic_json(path, state)
    authority = _active_authority(root)
    with authority._parent(relative, create=True) as (outcome_parent_fd, _):
        exit_code, stdout, stderr = _execute(
            command,
            root,
            runner=runner,
            pass_fds=(outcome_parent_fd,),
            extra_env={"KEG_EVIDENCE_DIR_FD": str(outcome_parent_fd)},
        )
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

    _assert_immutable_inputs_current(root, state)

    outcome_path = _canonical_out_path(
        root,
        outcome_path,
        relative,
        require_file=True,
    )
    raw = _read_evidence_bytes(outcome_path)
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


@_serialized_transition
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
    _assert_immutable_inputs_current(root, state)
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
        "status": "in_progress",
        "command": command,
        "cwd": os.fspath(root),
        "makefile_sha256": _sha256((root / "Makefile").read_bytes()),
        "started_at_utc": utc_iso(observed_start),
        "input_source_sha256": source_sha,
        "after_repair": state["stages"]["repair"]["status"] == "passed",
    }
    attempts.append(attempt)
    state["stages"]["verification"] = _stage(
        "in_progress",
        observed_start,
        state["run_id"],
        source_sha,
        {"attempts": attempts},
    )
    _atomic_json(path, state)
    exit_code, stdout, stderr = _execute(command, root, runner=runner)
    observed_finish = parse_utc(now or _now())
    assert observed_finish is not None
    _, _, _, identity_after = _load_sources(root)
    _assert_immutable_inputs_current(root, state)
    if identity_after != identity_before:
        exit_code = 98
    status = "passed" if exit_code == 0 and identity_after == identity_before else "failed"
    attempt.update(
        {
            "status": status,
            "finished_at_utc": utc_iso(observed_finish),
            "exit_code": exit_code,
            "stdout_sha256": _sha256(stdout),
            "stderr_sha256": _sha256(stderr),
            "output_source_sha256": identity_after["source_sha256"],
        }
    )
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


@_serialized_transition
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
    _assert_immutable_inputs_current(root, state)
    history = root / "history.csv"
    tool = root / "scripts/repair_history.py"
    _assert_plain_path(history)
    _assert_plain_path(tool)
    command = [PYTHON, os.fspath(tool), "--path", os.fspath(history), "--check"]
    observed_start = parse_utc(now or _now())
    assert observed_start is not None
    source_sha = state["stages"]["freshness"]["source_sha"]
    proposal_evidence = {
        "repair_id": repair_id,
        "action": "remove_only_estimated_history_rows",
        "target_path": "history.csv",
        "target_sha256_before": _sha256(history.read_bytes()),
        "tool_path": "scripts/repair_history.py",
        "tool_sha256": _sha256(tool.read_bytes()),
        "precheck_command": command,
        "precheck_started_at_utc": utc_iso(observed_start),
        "max_attempts": 1,
        "attempts_used": 0,
    }
    state["stages"]["repair"] = _stage(
        "proposal_in_progress",
        observed_start,
        state["run_id"],
        source_sha,
        proposal_evidence,
    )
    _atomic_json(path, state)
    exit_code, stdout, stderr = _execute(command, root, runner=runner)
    _assert_immutable_inputs_current(root, state)
    match = re.fullmatch(rb"(\d+) kept, (\d+) would remove\s*", stdout)
    if exit_code != 1 or not match or int(match.group(2)) <= 0:
        raise RunEvidenceError("history-prune precheck did not prove one applicable repair")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    proposal_evidence.update(
        {
            "precheck_finished_at_utc": utc_iso(observed_at),
            "precheck_exit_code": exit_code,
            "precheck_stdout_sha256": _sha256(stdout),
            "precheck_stderr_sha256": _sha256(stderr),
            "kept_count": int(match.group(1)),
            "remove_count": int(match.group(2)),
        }
    )
    state["stages"]["repair"] = _stage(
        "proposed",
        observed_at,
        state["run_id"],
        source_sha,
        proposal_evidence,
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


@_serialized_transition
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
    _assert_immutable_inputs_current(root, state)
    history = root / "history.csv"
    tool = root / "scripts/repair_history.py"
    if _sha256(history.read_bytes()) != evidence["target_sha256_before"]:
        raise RunEvidenceError("repair target drifted after proposal")
    if _sha256(tool.read_bytes()) != evidence["tool_sha256"]:
        raise RunEvidenceError("repair tool drifted after proposal")
    command = [PYTHON, os.fspath(tool), "--path", os.fspath(history)]
    observed_start = parse_utc(now or _now())
    assert observed_start is not None
    in_progress_evidence = {
        **evidence,
        "attempts_used": 1,
        "repair_command": command,
        "repair_started_at_utc": utc_iso(observed_start),
    }
    state["stages"]["repair"] = _stage(
        "in_progress",
        observed_start,
        state["run_id"],
        repair["source_sha"],
        in_progress_evidence,
    )
    _atomic_json(path, state)
    exit_code, stdout, stderr = _execute(command, root, runner=runner)
    _assert_immutable_inputs_current(root, state)
    check_command = [PYTHON, os.fspath(tool), "--path", os.fspath(history), "--check"]
    check_exit, check_stdout, check_stderr = _execute(check_command, root, runner=runner)
    _assert_immutable_inputs_current(root, state)
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    result_evidence = {
        **in_progress_evidence,
        "repair_finished_at_utc": utc_iso(observed_at),
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


@_serialized_transition
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
    if state["stages"]["repair"]["status"] in {
        "proposal_in_progress",
        "proposed",
        "in_progress",
        "failed",
        "review_required",
    }:
        raise RunEvidenceError("result_sha cannot bind an incomplete or failed repair")
    if _git_text(root, "branch", "--show-current") != "main":
        raise RunEvidenceError("result must remain on main")
    if not _status_is_clean(root):
        raise RunEvidenceError("result_sha cannot bind a dirty tracked or untracked worktree")
    _assert_immutable_inputs_current(root, state)
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
    _assert_result_provenance(root, state)
    changed_raw = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        state["start_sha"],
        result_sha,
        text=False,
    ).stdout
    changed_paths = sorted(value.decode("utf-8") for value in changed_raw.split(b"\0") if value)
    state["stages"]["preflight"]["evidence"]["result_bound_at_utc"] = utc_iso(observed_at)
    state["stages"]["preflight"]["evidence"]["result_origin"] = origin
    state["stages"]["preflight"]["evidence"]["changed_paths"] = changed_paths
    _atomic_json(path, state)
    return state


@_serialized_transition
def record_deployment_evidence(
    root: Path,
    state_path: Path | str,
    *,
    now: datetime | None = None,
    verifier: Callable[..., tuple[dict, dict]] | None = None,
    origin_reader: OriginReader = _read_live_origin,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if not state["result_sha"]:
        raise RunEvidenceError("result_sha must be bound before deployment verification")
    if state["stages"]["freshness"]["status"] != "passed":
        raise RunEvidenceError("deployment cannot pass for a non-successful refresh")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    origin_before = _assert_clean_bound_result(root, state, observed_at, origin_reader)
    _assert_result_source_blobs(root, state)
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
    origin_after = _assert_clean_bound_result(root, state, observed_at, origin_reader)
    _assert_result_source_blobs(root, state)
    if origin_after != origin_before:
        raise RunEvidenceError("live origin evidence changed during deployment verification")
    expected_proof_origin = {
        "repository": origin_after["repository"],
        "fetch_url": origin_after["fetch_url"],
        "push_url": origin_after["push_url"],
        "live_main_sha": origin_after["live_main_sha"],
    }
    if proof["origin"] != expected_proof_origin:
        raise RunEvidenceError("deployment verifier origin differs from the live bound result")
    state["stages"]["deployment"] = _stage(
        "passed", observed_at, state["run_id"], source_sha, proof
    )
    _atomic_json(path, state)
    return state


@_serialized_transition
def record_payload_evidence(
    root: Path,
    state_path: Path | str,
    payload_path: Path | str,
    *,
    now: datetime | None = None,
    origin_reader: OriginReader = _read_live_origin,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["deployment"]["status"] != "passed":
        raise RunEvidenceError("deployment must be proven before payload evidence is accepted")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    origin = _assert_clean_bound_result(root, state, observed_at, origin_reader)
    _assert_result_source_blobs(root, state)
    payload, raw, current_identity, generated_at = _validate_current_payload(
        root,
        state,
        payload_path,
        observed_at,
    )
    evidence = {
        "payload_sha256": _sha256(raw),
        "payload_file": "out/latest-email.json",
        "generated_at_utc": utc_iso(generated_at),
        "to": payload["to"],
        "cc": payload["cc"],
        "bcc": payload["bcc"],
        "subject": payload["subject"],
        "source_identity": current_identity,
        "origin": origin,
    }
    state["stages"]["payload"] = _stage(
        "passed",
        observed_at,
        state["run_id"],
        current_identity["source_sha256"],
        evidence,
    )
    state["stages"]["pre_send"] = _stage(
        "unverified", observed_at, state["run_id"], None
    )
    _atomic_json(path, state)
    return state


def _validate_current_payload(
    root: Path,
    state: dict,
    payload_path: Path | str,
    observed_at: datetime,
) -> tuple[dict, bytes, dict, datetime]:
    """Validate canonical bytes, age, sources, and the exact recipient boundary."""
    payload_file = _canonical_out_path(
        root,
        payload_path,
        Path("out/latest-email.json"),
        require_file=True,
    )
    raw = _read_evidence_bytes(payload_file)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunEvidenceError(f"email payload evidence is invalid: {exc}") from exc
    listings, specs, refresh, current_identity = _load_sources(root)
    recorded_identity = state["stages"]["freshness"]["evidence"].get("output_source_identity")
    if current_identity != recorded_identity:
        raise RunEvidenceError("email payload inputs drifted after the current refresh")
    generated_at = parse_utc(payload.get("generated_at") if isinstance(payload, dict) else None)
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
    if payload["to"] != list(EXPECTED_RECIPIENTS) or payload["cc"] != [] or payload["bcc"] != []:
        raise RunEvidenceError("email payload recipients are outside the exact approved boundary")
    return payload, raw, current_identity, generated_at


def _assert_payload_matches_binding(
    state: dict,
    payload: dict,
    raw: bytes,
    current_identity: dict,
    generated_at: datetime,
) -> None:
    recorded = state["stages"]["payload"]["evidence"]
    expected = {
        "payload_sha256": _sha256(raw),
        "payload_file": "out/latest-email.json",
        "generated_at_utc": utc_iso(generated_at),
        "to": payload["to"],
        "cc": payload["cc"],
        "bcc": payload["bcc"],
        "subject": payload["subject"],
        "source_identity": current_identity,
    }
    for field, value in expected.items():
        if recorded.get(field) != value:
            raise RunEvidenceError(f"current payload drifted from its recorded binding: {field}")


@_serialized_transition
def record_pre_send_validation(
    root: Path,
    state_path: Path | str,
    payload_path: Path | str = Path("out/latest-email.json"),
    *,
    now: datetime | None = None,
    origin_reader: OriginReader = _read_live_origin,
) -> dict:
    """Perform the mandatory immediate check immediately before the one send attempt."""
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    if state["stages"]["payload"]["status"] != "passed":
        raise RunEvidenceError("pre-send validation requires a passed payload binding")
    if state["stages"]["pre_send"]["status"] == "passed":
        raise RunEvidenceError("pre-send validation may be recorded only once")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    payload_bound_at = parse_utc(state["stages"]["payload"]["observed_at_utc"])
    assert payload_bound_at is not None
    if observed_at < payload_bound_at or observed_at - payload_bound_at > MAX_PRE_SEND_BIND_AGE:
        raise RunEvidenceError("payload binding is too old for immediate pre-send validation")
    origin = _assert_clean_bound_result(root, state, observed_at, origin_reader)
    _assert_result_source_blobs(root, state)
    payload, raw, current_identity, generated_at = _validate_current_payload(
        root, state, payload_path, observed_at
    )
    _assert_payload_matches_binding(state, payload, raw, current_identity, generated_at)
    evidence = {
        "payload_sha256": _sha256(raw),
        "payload_file": "out/latest-email.json",
        "validated_at_utc": utc_iso(observed_at),
        "generated_at_utc": utc_iso(generated_at),
        "to": payload["to"],
        "cc": payload["cc"],
        "bcc": payload["bcc"],
        "subject": payload["subject"],
        "source_identity": current_identity,
        "origin": origin,
    }
    state["stages"]["pre_send"] = _stage(
        "passed",
        observed_at,
        state["run_id"],
        current_identity["source_sha256"],
        evidence,
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


def _revalidate_pre_send_at_finish(
    root: Path,
    state: dict,
    observed_at: datetime,
    origin_reader: OriginReader,
) -> dict:
    pre_send = state["stages"]["pre_send"]
    if pre_send["status"] != "passed":
        raise RunEvidenceError("delivery_unverified requires immediate pre-send validation")
    validated_at = parse_utc(pre_send["evidence"].get("validated_at_utc"))
    if validated_at is None or observed_at < validated_at:
        raise RunEvidenceError("finish timestamp predates pre-send validation")
    if observed_at - validated_at > MAX_FINISH_AFTER_PRE_SEND:
        raise RunEvidenceError("pre-send validation is too old to finish this send attempt")
    origin = _assert_clean_bound_result(root, state, observed_at, origin_reader)
    _assert_result_source_blobs(root, state)
    payload, raw, identity, generated_at = _validate_current_payload(
        root,
        state,
        root / "out/latest-email.json",
        observed_at,
    )
    _assert_payload_matches_binding(state, payload, raw, identity, generated_at)
    expected_pre_send = {
        "payload_sha256": _sha256(raw),
        "payload_file": "out/latest-email.json",
        "validated_at_utc": pre_send["observed_at_utc"],
        "generated_at_utc": utc_iso(generated_at),
        "to": payload["to"],
        "cc": payload["cc"],
        "bcc": payload["bcc"],
        "subject": payload["subject"],
        "source_identity": identity,
    }
    for field, value in expected_pre_send.items():
        if pre_send["evidence"].get(field) != value:
            raise RunEvidenceError(f"current payload drifted after pre-send validation: {field}")
    recorded_origin = pre_send["evidence"].get("origin") or {}
    for field in ("repository", "fetch_url", "push_url", "live_main_sha"):
        if recorded_origin.get(field) != origin.get(field):
            raise RunEvidenceError(f"origin drifted after pre-send validation: {field}")
    return origin


@_serialized_transition
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
    if outcome == "failed" and state["stages"]["blocker"]["status"] != "recorded":
        raise RunEvidenceError("failed run requires recorded blocker evidence")
    if outcome == "delivery_unverified":
        for stage_name in ("freshness", "verification", "deployment", "payload", "pre_send"):
            if state["stages"][stage_name]["status"] != "passed":
                raise RunEvidenceError(f"delivery_unverified requires {stage_name}=passed")
        if state["stages"]["receipt"]["status"] != "unverified":
            raise RunEvidenceError("delivery_unverified must retain receipt=unverified")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    if observed_at < parse_utc(state["started_at_utc"]):
        raise RunEvidenceError("finish timestamp predates run start")
    if outcome == "delivery_unverified":
        state["origin_at_finish"] = _revalidate_pre_send_at_finish(
            root, state, observed_at, origin_reader
        )
    elif outcome == "blocked":
        state["origin_at_finish"] = _assert_clean_bound_result(
            root, state, observed_at, origin_reader
        )
    else:
        _mark_interrupted_attempts_review_required(
            state, observed_at, "run finalized while an owned command was in progress"
        )
        state["origin_at_finish"] = _finish_origin(
            root,
            state,
            observed_at,
            origin_reader,
            allow_unverified=True,
        )
    state["finished_at_utc"] = utc_iso(observed_at)
    state["status"] = outcome
    _atomic_json(path, state)
    return state


@_serialized_transition
def finalize_failure(
    root: Path,
    state_path: Path | str,
    failure_stage: str,
    reason_code: str,
    *,
    detail: object = "fixed command failed",
    now: datetime | None = None,
    origin_reader: OriginReader = _read_live_origin,
    preserve_first_verification_failure: bool = False,
) -> dict:
    root = _exact_repo_root(root)
    path, state = _load_state(root, state_path)
    _require_running(state)
    attempts = state["stages"]["verification"]["evidence"].get("attempts") or []
    if preserve_first_verification_failure and (
        state["stages"]["verification"]["status"] == "failed"
        and len(attempts) == 1
        and state["stages"]["repair"]["status"] == "not_required"
    ):
        return state
    failure_stage = _validate_id(failure_stage, "failure_stage")
    reason_code = _validate_id(reason_code, "reason_code")
    observed_at = parse_utc(now or _now())
    assert observed_at is not None
    _mark_interrupted_attempts_review_required(
        state, observed_at, "adapter failure interrupted an owned command"
    )
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


@_serialized_transition
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
    expected_origin_sha = state["result_sha"] or state["start_sha"]
    if origin["live_main_sha"] != expected_origin_sha:
        raise RunEvidenceError(
            "stale recovery requires live origin/main to match the last bound run SHA"
        )
    _mark_interrupted_attempts_review_required(
        state, observed_at, "stale recovery found an interrupted owned command"
    )
    state["recovery"] = {
        "status": "stale_owner_recovered",
        "observed_at_utc": utc_iso(observed_at),
        "expected_run_id": expected_run_id,
        "minimum_age_seconds": int(STALE_RUN_AGE.total_seconds()),
        "age_seconds": int((observed_at - started_at).total_seconds()),
        "owner": owner,
        "expected_origin_sha": expected_origin_sha,
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
    try:
        finalize_failure(
            root,
            state_path,
            command,
            "adapter_command_failed",
            detail=f"{type(exc).__name__}: {_bounded_reason(exc)}",
            preserve_first_verification_failure=command == "verify",
        )
    except Exception:
        pass


def _print_summary(state: dict) -> None:
    blocker = state["stages"]["blocker"]
    blocker_value = blocker["status"]
    if blocker_value == "recorded":
        blocker_value = f"recorded:{blocker['evidence']['reason_code']}"
    print(
        f"run_id={state['run_id']} workflow_id={state['workflow_id']} "
        f"lane_id={state['lane_id']} status={state['status']} "
        f"start_sha={state['start_sha']} result_sha={state['result_sha'] or 'unbound'} "
        f"freshness={state['stages']['freshness']['status']} "
        f"verification={state['stages']['verification']['status']} "
        f"deployment={state['stages']['deployment']['status']} "
        f"payload={state['stages']['payload']['status']} "
        f"pre_send={state['stages']['pre_send']['status']} "
        f"blocker={blocker_value} "
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
    pre_send = commands.add_parser("pre-send")
    pre_send.add_argument("--payload", type=Path, default=Path("out/latest-email.json"))
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
        elif args.command == "pre-send":
            state = record_pre_send_validation(root, args.state, args.payload)
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
