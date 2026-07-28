#!/usr/bin/env python3
"""Check the public GitHub Pages dashboard."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audience_guard import (  # noqa: E402
    AudienceBoundaryError,
    listing_source_urls,
    validate_html,
)
from scripts.refresh_state import (  # noqa: E402
    build_payload_source_identity,
    evaluate_refresh,
    utc_iso,
    validate_refresh_status,
)


PUBLIC_URL = "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/"
PUBLIC_LISTINGS_URL = urljoin(PUBLIC_URL, "data/listings.json")
PUBLIC_STATUS_URL = urljoin(PUBLIC_URL, "data/refresh-status.json")
DEPLOYED_PATHS = (
    "index.html",
    "assets/kegerator-hero.png",
    "data/listings.json",
    "data/specs.json",
    "data/refresh-status.json",
    "history.csv",
)
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")


def fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "LukeKegeratorTracker/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read()


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


def validate_deployment_bundle(
    local_files: dict[str, bytes],
    remote_files: dict[str, bytes],
    *,
    result_sha: str,
    origin_main_sha: str,
    source_sha: str,
) -> dict:
    """Bind exact public bytes to the pushed result and current refresh identity."""
    expected_paths = set(DEPLOYED_PATHS)
    if set(local_files) != expected_paths or set(remote_files) != expected_paths:
        raise AudienceBoundaryError("deployment bundle paths are incomplete or unexpected")
    if not COMMIT_RE.fullmatch(str(result_sha or "")):
        raise AudienceBoundaryError("deployment result_sha is invalid")
    if origin_main_sha != result_sha:
        raise AudienceBoundaryError("origin/main is not the expected local result_sha")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source_sha or "")):
        raise AudienceBoundaryError("deployment source_sha is invalid")
    digests: dict[str, str] = {}
    for relative in DEPLOYED_PATHS:
        local_raw = local_files[relative]
        remote_raw = remote_files[relative]
        if not isinstance(local_raw, bytes) or not isinstance(remote_raw, bytes):
            raise AudienceBoundaryError("deployment bundle comparison requires exact bytes")
        if remote_raw != local_raw:
            raise AudienceBoundaryError(f"public deployment differs from local HEAD bytes: {relative}")
        digests[relative] = _sha256(local_raw)
    marker_payload = json.dumps(
        {
            "result_sha": result_sha,
            "source_sha": source_sha,
            "files": digests,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "result_sha": result_sha,
        "origin_main_sha": origin_main_sha,
        "source_sha": source_sha,
        "bundle_marker_sha256": _sha256(marker_payload),
        "files": digests,
        "public_url": PUBLIC_URL,
    }


def verify_public_deployment(
    root: Path = ROOT,
    *,
    expected_sha: str | None = None,
    fetcher=fetch,
) -> tuple[dict, dict]:
    """Verify semantic boundaries and exact deployed bytes against local HEAD."""
    root = root.resolve()
    if _git_text(root, "rev-parse", "--show-toplevel") != os.fspath(root):
        raise AudienceBoundaryError("deployment check root must be the exact git repository root")
    head_sha = _git_text(root, "rev-parse", "HEAD")
    expected_sha = expected_sha or head_sha
    if expected_sha != head_sha:
        raise AudienceBoundaryError("local HEAD does not match the expected run result_sha")
    remote_line = _git_text(root, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    origin_main_sha = remote_line.split()[0] if remote_line else ""
    local_files: dict[str, bytes] = {}
    for relative in DEPLOYED_PATHS:
        if _git(root, "ls-files", "--error-unmatch", "--", relative, check=False).returncode != 0:
            raise AudienceBoundaryError(f"deployment candidate is not tracked at HEAD: {relative}")
        if _git(root, "diff", "--quiet", "HEAD", "--", relative, check=False).returncode != 0:
            raise AudienceBoundaryError(f"deployment candidate has uncommitted drift: {relative}")
        local_files[relative] = (root / relative).read_bytes()
    remote_files: dict[str, bytes] = {}
    for relative in DEPLOYED_PATHS:
        status, raw = fetcher(urljoin(PUBLIC_URL, relative))
        if status != 200:
            raise AudienceBoundaryError(f"unexpected public status {status}: {relative}")
        remote_files[relative] = raw
    listings = json.loads(remote_files["data/listings.json"].decode("utf-8"))
    specs = json.loads(local_files["data/specs.json"].decode("utf-8"))
    refresh_status = json.loads(remote_files["data/refresh-status.json"].decode("utf-8"))
    state = validate_public_status(refresh_status, listings)
    validate_public_body(
        remote_files["index.html"],
        listing_source_urls(listings),
        refresh_status,
    )
    local_listings = json.loads(local_files["data/listings.json"].decode("utf-8"))
    local_refresh = json.loads(local_files["data/refresh-status.json"].decode("utf-8"))
    source_sha = build_payload_source_identity(local_listings, specs, local_refresh)["source_sha256"]
    proof = validate_deployment_bundle(
        local_files,
        remote_files,
        result_sha=expected_sha,
        origin_main_sha=origin_main_sha,
        source_sha=source_sha,
    )
    proof["public_state"] = state["state"]
    proof["data_refreshed_at_utc"] = state["data_refreshed_at_utc"]
    return state, proof


def validate_public_body(
    body: bytes,
    allowed_listing_urls: set[str] | frozenset[str],
    refresh_status: dict,
) -> None:
    if not isinstance(body, bytes):
        raise AudienceBoundaryError("public dashboard must be validated as exact response bytes")
    validate_html(
        body,
        allowed_listing_urls=allowed_listing_urls,
        asset_root=ROOT,
        source_path=PUBLIC_URL,
    )
    text = body.decode("utf-8")
    validate_refresh_status(refresh_status)
    required = [
        "Kegerator Tracker",
        "data/listings.json",
        "data/specs.json",
        "data/refresh-status.json",
        "history.csv",
        "data_refreshed_at_utc",
        "Last successful data refresh",
        "Historical only",
    ]
    missing = [value for value in required if value not in text]
    if missing:
        raise AudienceBoundaryError(f"public dashboard missing: {missing}")


def validate_public_status(
    refresh_status: dict,
    listings: list[dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Prove public metadata represents the successful snapshot and visible state."""
    refresh = validate_refresh_status(refresh_status)
    if not isinstance(listings, list) or not listings:
        raise AudienceBoundaryError("public listings must be a non-empty array")
    if refresh["source_count"] != len(listings) or refresh["row_count"] != len(listings):
        raise AudienceBoundaryError("public refresh counts do not match public listings")
    expected_quality = {"verified": len(listings), "estimated": 0, "blocked": 0}
    if refresh["quality_counts"] != expected_quality:
        raise AudienceBoundaryError("public refresh quality counts do not represent the successful snapshot")
    success_at = refresh["data_refreshed_at_utc"]
    if not success_at:
        raise AudienceBoundaryError("public status must record a successful data refresh")
    for index, row in enumerate(listings):
        if row.get("data_quality") != "confirmed":
            raise AudienceBoundaryError(f"public listing {index} is not confirmed historical evidence")
        if utc_iso(row.get("retrieved")) != success_at:
            raise AudienceBoundaryError(f"public listing {index} is not from the successful snapshot")
    return evaluate_refresh(refresh, now=now or datetime.now(timezone.utc))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--expected-sha")
    parser.add_argument("--run-state", type=Path)
    args = parser.parse_args()
    expected_sha = args.expected_sha
    run_state_path = args.run_state
    if args.run_state:
        if not run_state_path.is_absolute():
            run_state_path = args.root.resolve() / run_state_path
        run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
        state_sha = run_state.get("result_sha")
        if expected_sha is not None and expected_sha != state_sha:
            raise AudienceBoundaryError("run-state result_sha conflicts with --expected-sha")
        expected_sha = state_sha
    state, proof = verify_public_deployment(args.root, expected_sha=expected_sha)
    if args.run_state:
        from scripts.run_evidence import record_deployment_proof

        record_deployment_proof(args.root.resolve(), run_state_path, proof)
    print(
        f"public dashboard ok: {PUBLIC_URL} state={state['state']} "
        f"data_refreshed_at={state['data_refreshed_at_utc']} "
        f"result_sha={proof['result_sha']} marker={proof['bundle_marker_sha256']}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"public dashboard check failed: {exc}", file=sys.stderr)
        raise
