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
    parse_utc,
    utc_iso,
    validate_refresh_status,
)


PUBLIC_URL = "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/"
PUBLIC_LISTINGS_URL = urljoin(PUBLIC_URL, "data/listings.json")
PUBLIC_STATUS_URL = urljoin(PUBLIC_URL, "data/refresh-status.json")
EXPECTED_ORIGIN_REPOSITORY = "lukestambaugh75-hue/kegerator-tracker-r0"
ALLOWED_ORIGIN_URLS = {
    "https://github.com/lukestambaugh75-hue/kegerator-tracker-r0.git",
    "git@github.com:lukestambaugh75-hue/kegerator-tracker-r0.git",
    "ssh://git@github.com/lukestambaugh75-hue/kegerator-tracker-r0.git",
}
DEPLOYED_PATHS = (
    "index.html",
    "assets/kegerator-hero.png",
    "data/listings.json",
    "data/specs.json",
    "data/refresh-status.json",
    "history.csv",
)
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")


def fetch(url: str) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "LukeKegeratorTracker/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status, response.read(), response.geturl()


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


def _git_bytes(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        capture_output=True,
        check=True,
    ).stdout


def _live_origin(root: Path) -> dict:
    fetch_url = _git_text(root, "remote", "get-url", "origin")
    push_url = _git_text(root, "remote", "get-url", "--push", "origin")
    if fetch_url not in ALLOWED_ORIGIN_URLS or push_url not in ALLOWED_ORIGIN_URLS:
        raise AudienceBoundaryError("origin URL does not identify the approved Kegerator repository")
    remote_line = _git_text(root, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    rows = [line.split() for line in remote_line.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != "refs/heads/main":
        raise AudienceBoundaryError("live origin/main identity is unavailable or ambiguous")
    sha = rows[0][0]
    if not COMMIT_RE.fullmatch(sha):
        raise AudienceBoundaryError("live origin/main returned an invalid commit identity")
    return {
        "repository": EXPECTED_ORIGIN_REPOSITORY,
        "fetch_url": fetch_url,
        "push_url": push_url,
        "live_main_sha": sha,
    }


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
    origin = _live_origin(root)
    origin_main_sha = origin["live_main_sha"]
    local_files: dict[str, bytes] = {}
    for relative in DEPLOYED_PATHS:
        if _git(root, "ls-files", "--error-unmatch", "--", relative, check=False).returncode != 0:
            raise AudienceBoundaryError(f"deployment candidate is not tracked at HEAD: {relative}")
        local_files[relative] = _git_bytes(root, "show", f"{expected_sha}:{relative}")
    remote_files: dict[str, bytes] = {}
    fetches: dict[str, dict] = {}
    for relative in DEPLOYED_PATHS:
        requested_url = urljoin(PUBLIC_URL, relative)
        response = fetcher(requested_url)
        if not isinstance(response, tuple) or len(response) != 3:
            raise AudienceBoundaryError("public fetcher must return status, exact bytes, and final URL")
        status, raw, final_url = response
        if status != 200:
            raise AudienceBoundaryError(f"unexpected public status {status}: {relative}")
        if final_url != requested_url:
            raise AudienceBoundaryError(f"public fetch redirected away from the canonical URL: {relative}")
        if not isinstance(raw, bytes):
            raise AudienceBoundaryError(f"public fetch did not return exact bytes: {relative}")
        remote_files[relative] = raw
        fetches[relative] = {
            "url": requested_url,
            "final_url": final_url,
            "status": status,
            "sha256": _sha256(raw),
        }
    listings = json.loads(remote_files["data/listings.json"].decode("utf-8"))
    specs = json.loads(remote_files["data/specs.json"].decode("utf-8"))
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
    proof["fetches"] = fetches
    proof["origin"] = origin
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
    """Prove public metadata matches every row's current evidence state."""
    refresh = validate_refresh_status(refresh_status)
    if not isinstance(listings, list) or not listings:
        raise AudienceBoundaryError("public listings must be a non-empty array")
    if refresh["source_count"] != len(listings) or refresh["row_count"] != len(listings):
        raise AudienceBoundaryError("public refresh counts do not match public listings")
    row_quality = {
        "verified": sum(row.get("data_quality") == "confirmed" for row in listings),
        "estimated": sum(row.get("data_quality") == "estimated" for row in listings),
        "blocked": sum(row.get("data_quality") == "blocked" for row in listings),
    }
    if sum(row_quality.values()) != len(listings) or refresh["quality_counts"] != row_quality:
        raise AudienceBoundaryError("public refresh quality counts do not match listing provenance")
    success_at = refresh["data_refreshed_at_utc"]
    if not success_at:
        raise AudienceBoundaryError("public status must record a successful data refresh")
    attempt_at = parse_utc(refresh["last_attempt_at_utc"])
    for index, row in enumerate(listings):
        retrieved = parse_utc(row.get("retrieved"))
        if retrieved is None or (attempt_at is not None and retrieved > attempt_at):
            raise AudienceBoundaryError(f"public listing {index} has invalid retrieval evidence")
        if refresh["last_attempt_status"] == "success":
            if row.get("data_quality") != "confirmed" or utc_iso(retrieved) != success_at:
                raise AudienceBoundaryError(f"public listing {index} is not from the successful snapshot")
        elif refresh["last_attempt_status"] == "partial":
            if row.get("data_quality") == "confirmed" and retrieved != attempt_at:
                raise AudienceBoundaryError(f"public listing {index} is not current confirmed evidence")
            if row.get("data_quality") == "blocked" and retrieved == attempt_at:
                raise AudienceBoundaryError(f"public listing {index} falsely claims current evidence")
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
        if expected_sha is not None:
            raise AudienceBoundaryError("--expected-sha cannot be combined with --run-state")
        from scripts.run_evidence import record_deployment_evidence

        run_state = record_deployment_evidence(args.root.resolve(), run_state_path)
        proof = run_state["stages"]["deployment"]["evidence"]
        state = {
            "state": proof["public_state"],
            "data_refreshed_at_utc": proof["data_refreshed_at_utc"],
        }
    else:
        state, proof = verify_public_deployment(args.root, expected_sha=expected_sha)
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
