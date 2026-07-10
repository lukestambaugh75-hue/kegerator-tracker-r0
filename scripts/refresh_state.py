#!/usr/bin/env python3
"""Pure refresh-state evaluation and snapshot-preservation rules."""

from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


CENTRAL_ZONE = ZoneInfo("America/Chicago")
DEFAULT_CADENCE_MINUTES = 1440
DEFAULT_GRACE_MINUTES = 180
FAILURE_STATUSES = {"blocked", "partial", "failed"}
ATTEMPT_STATUSES = {"success", "unknown", *FAILURE_STATUSES}


def parse_utc(value: str | datetime | None) -> datetime | None:
    """Parse an ISO timestamp and normalize it to aware UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"UTC timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def utc_iso(value: str | datetime | None) -> str | None:
    """Return the repository's canonical UTC timestamp form."""
    parsed = parse_utc(value)
    if parsed is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def format_central(value: str | datetime | None) -> str:
    """Format a timestamp in America/Chicago with a truthful CST/CDT label."""
    parsed = parse_utc(value)
    if parsed is None:
        return "Not recorded"
    local = parsed.astimezone(CENTRAL_ZONE)
    hour = local.strftime("%I").lstrip("0") or "0"
    return f"{local.strftime('%b')} {local.day}, {local.year} {hour}:{local.strftime('%M %p %Z')}"


def format_age(age_minutes: int | None) -> str:
    if age_minutes is None:
        return "Unknown"
    total = max(0, int(age_minutes))
    days, remainder = divmod(total, 1440)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if not parts or (not days and minutes):
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts)


def validate_refresh_status(refresh: dict) -> dict:
    """Validate and normalize durable refresh metadata."""
    if not isinstance(refresh, dict):
        raise ValueError("refresh status must be an object")
    required = {
        "data_refreshed_at_utc",
        "last_attempt_at_utc",
        "last_attempt_status",
        "last_attempt_reason",
        "cadence_minutes",
        "grace_minutes",
        "timezone",
        "archived",
        "source_count",
        "row_count",
        "quality_counts",
        "rendered_at_utc",
        "published_at_utc",
    }
    missing = sorted(required - set(refresh))
    if missing:
        raise ValueError(f"refresh status missing fields: {missing}")

    normalized = copy.deepcopy(refresh)
    normalized["data_refreshed_at_utc"] = utc_iso(refresh.get("data_refreshed_at_utc"))
    normalized["last_attempt_at_utc"] = utc_iso(refresh.get("last_attempt_at_utc"))
    normalized["rendered_at_utc"] = utc_iso(refresh.get("rendered_at_utc"))
    normalized["published_at_utc"] = utc_iso(refresh.get("published_at_utc"))
    status = str(refresh.get("last_attempt_status") or "unknown").lower()
    if status not in ATTEMPT_STATUSES:
        raise ValueError(f"invalid last_attempt_status: {status}")
    normalized["last_attempt_status"] = status
    reason = refresh.get("last_attempt_reason")
    normalized["last_attempt_reason"] = str(reason).strip() if reason not in (None, "") else None
    if status in FAILURE_STATUSES and not normalized["last_attempt_reason"]:
        raise ValueError(f"{status} refresh status requires a reason")

    cadence = refresh.get("cadence_minutes")
    grace = refresh.get("grace_minutes")
    if not isinstance(cadence, int) or isinstance(cadence, bool) or cadence <= 0:
        raise ValueError("cadence_minutes must be a positive integer")
    if not isinstance(grace, int) or isinstance(grace, bool) or grace < 0:
        raise ValueError("grace_minutes must be a non-negative integer")
    if refresh.get("timezone") != "America/Chicago":
        raise ValueError("refresh timezone must be America/Chicago")
    if not isinstance(refresh.get("archived"), bool):
        raise ValueError("archived must be boolean")

    for field in ("source_count", "row_count"):
        value = refresh.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    quality = refresh.get("quality_counts")
    if not isinstance(quality, dict) or set(quality) != {"verified", "estimated", "blocked"}:
        raise ValueError("quality_counts must contain verified, estimated, and blocked")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in quality.values()):
        raise ValueError("quality_counts values must be non-negative integers")
    if sum(quality.values()) != refresh["row_count"]:
        raise ValueError("quality_counts must equal row_count")
    if refresh["source_count"] != refresh["row_count"]:
        raise ValueError("source_count must equal row_count for the durable successful snapshot")

    success_at = parse_utc(normalized["data_refreshed_at_utc"])
    attempt_at = parse_utc(normalized["last_attempt_at_utc"])
    if success_at is not None:
        if refresh["row_count"] <= 0:
            raise ValueError("a successful data refresh requires a positive row_count")
        expected_quality = {
            "verified": refresh["row_count"],
            "estimated": 0,
            "blocked": 0,
        }
        if quality != expected_quality:
            raise ValueError("durable successful snapshot quality must be fully verified")
    elif refresh["row_count"] != 0 or any(quality.values()):
        raise ValueError("status without a successful refresh cannot claim snapshot rows")

    if status == "success":
        if success_at is None:
            raise ValueError("a successful attempt requires data_refreshed_at_utc")
        if attempt_at is None:
            raise ValueError("a successful attempt requires last_attempt_at_utc")
        if attempt_at != success_at:
            raise ValueError("successful attempt timestamp must equal data_refreshed_at_utc")
        if normalized["last_attempt_reason"] is not None:
            raise ValueError("a successful attempt cannot retain a failure reason")
    elif status in FAILURE_STATUSES:
        if attempt_at is None:
            raise ValueError(f"{status} attempt requires last_attempt_at_utc")
        if success_at is not None and attempt_at <= success_at:
            raise ValueError(f"{status} attempt must be strictly newer than the successful data refresh")
    else:
        if success_at is not None:
            raise ValueError("unknown attempt status contradicts a successful data refresh")
        if attempt_at is not None or normalized["last_attempt_reason"] is not None:
            raise ValueError("unknown attempt status cannot retain attempt metadata")
    return normalized


def migrate_successful_snapshot(listings: list[dict], success_at: str | datetime) -> list[dict]:
    """Restore confirmed provenance without changing any other listing field."""
    if not isinstance(listings, list) or not listings:
        raise ValueError("migration listings must be a non-empty array")
    success_iso = utc_iso(success_at)
    migrated = copy.deepcopy(listings)
    for index, row in enumerate(migrated):
        if not isinstance(row, dict):
            raise ValueError(f"migration listing {index} must be an object")
        if utc_iso(row.get("retrieved")) != success_iso:
            raise ValueError(f"migration listing {index} is not from the successful snapshot")
        if row.get("data_quality") not in {"estimated", "confirmed"}:
            raise ValueError(f"migration listing {index} has unsupported provenance")
        row["data_quality"] = "confirmed"
    return migrated


def evaluate_refresh(refresh: dict, now: datetime | None = None) -> dict:
    """Derive Archived, Unknown, Blocked, Fresh, Due, or Stale."""
    refresh = validate_refresh_status(refresh)
    now = parse_utc(now or datetime.now(timezone.utc))
    assert now is not None
    success_at = parse_utc(refresh["data_refreshed_at_utc"])
    attempt_at = parse_utc(refresh["last_attempt_at_utc"])
    cadence = refresh["cadence_minutes"]
    grace = refresh["grace_minutes"]

    result = {
        **refresh,
        "state": "Unknown",
        "reason": "No successful data refresh is recorded.",
        "evaluated_at_utc": utc_iso(now),
        "data_refreshed_at_central": format_central(success_at),
        "last_attempt_at_central": format_central(attempt_at),
        "age_minutes": None,
        "age_label": "Unknown",
        "next_due_at_utc": None,
        "next_due_at_central": "Not recorded",
        "stale_after_at_utc": None,
        "stale_after_at_central": "Not recorded",
    }

    if refresh["archived"]:
        result["state"] = "Archived"
        result["reason"] = "This tracker is archived and no longer refreshes."
        return result

    if success_at is None:
        return result
    if success_at > now:
        result["reason"] = "The recorded successful data refresh is in the future."
        return result
    if attempt_at is not None and attempt_at > now:
        result["reason"] = "The recorded latest attempt is in the future."
        return result

    next_due = success_at + timedelta(minutes=cadence)
    stale_after = next_due + timedelta(minutes=grace)
    age_seconds = (now - success_at).total_seconds()
    age_minutes = max(0, int(age_seconds // 60))
    result.update(
        {
            "age_minutes": age_minutes,
            "age_label": format_age(age_minutes),
            "next_due_at_utc": utc_iso(next_due),
            "next_due_at_central": format_central(next_due),
            "stale_after_at_utc": utc_iso(stale_after),
            "stale_after_at_central": format_central(stale_after),
        }
    )

    if (
        refresh["last_attempt_status"] in FAILURE_STATUSES
        and attempt_at is not None
        and attempt_at > success_at
    ):
        result["state"] = "Blocked"
        result["reason"] = (
            f"Latest attempt {refresh['last_attempt_status']}: "
            f"{refresh['last_attempt_reason']}"
        )
        return result

    if age_seconds <= cadence * 60:
        result["state"] = "Fresh"
        result["reason"] = "Data is within the 24-hour refresh cadence."
    elif age_seconds <= (cadence + grace) * 60:
        result["state"] = "Due"
        result["reason"] = "Data is due but remains inside the 3-hour grace window."
    else:
        result["state"] = "Stale"
        result["reason"] = "Data is older than the cadence and grace window."
    return result


def apply_refresh_outcome(
    prior_listings: list[dict],
    prior_status: dict,
    candidate_listings: list[dict],
    outcome: dict,
    *,
    now: datetime | None = None,
) -> tuple[list[dict], dict, bool]:
    """Apply only a complete current success; otherwise preserve the snapshot."""
    status = validate_refresh_status(prior_status)
    if not isinstance(prior_listings, list) or not isinstance(candidate_listings, list):
        raise ValueError("listings must be arrays")
    if not isinstance(outcome, dict):
        raise ValueError("refresh outcome must be an object")
    outcome_status = str(outcome.get("status") or "").lower()
    if outcome_status not in {"success", *FAILURE_STATUSES}:
        raise ValueError(f"invalid refresh outcome status: {outcome_status}")

    attempted_at = parse_utc(outcome.get("attempted_at_utc"))
    if attempted_at is None:
        raise ValueError("refresh outcome requires attempted_at_utc")
    observed_at = parse_utc(now or datetime.now(timezone.utc))
    assert observed_at is not None
    if attempted_at > observed_at:
        raise ValueError("refresh evidence timestamp is in the future")
    prior_attempt = parse_utc(status.get("last_attempt_at_utc"))
    if prior_attempt is not None and attempted_at <= prior_attempt:
        raise ValueError("refresh attempt must be newer than the stored attempt")
    prior_success = parse_utc(status.get("data_refreshed_at_utc"))
    if outcome_status == "success" and prior_success is not None and attempted_at <= prior_success:
        raise ValueError("successful evidence must be newer than the stored success")

    expected_count = status["source_count"]
    if expected_count <= 0:
        raise ValueError("refresh requires a positive expected_count")
    if status["row_count"] != expected_count or len(prior_listings) != expected_count:
        raise ValueError("source_count, row_count, and prior listings must match expected_count")
    prior_identities = _stable_identity_set(prior_listings, "prior")
    confirmed = outcome.get("confirmed_count")
    failed = outcome.get("failed_count")
    if not isinstance(confirmed, int) or isinstance(confirmed, bool) or confirmed < 0:
        raise ValueError("confirmed_count must be a non-negative integer")
    if not isinstance(failed, int) or isinstance(failed, bool) or failed < 0:
        raise ValueError("failed_count must be a non-negative integer")
    if confirmed + failed != expected_count:
        raise ValueError("refresh outcome counts must equal the source count")
    if outcome_status == "success" and not (confirmed == expected_count and failed == 0):
        raise ValueError("success requires every target confirmed and zero failed")
    if outcome_status == "blocked" and not (confirmed == 0 and failed == expected_count):
        raise ValueError("blocked requires zero confirmed and every target failed")
    if outcome_status == "partial" and not (
        0 < confirmed < expected_count and failed == expected_count - confirmed
    ):
        raise ValueError("partial requires strictly between zero and every target confirmed")
    if outcome_status == "failed" and not (confirmed == 0 and failed == expected_count):
        raise ValueError("failed requires zero confirmed and every target failed")

    if outcome_status == "success":
        if len(candidate_listings) != expected_count:
            raise ValueError("success requires every target to be confirmed")
        candidate_identities = _stable_identity_set(candidate_listings, "candidate")
        if candidate_identities != prior_identities:
            raise ValueError("successful candidate identity set must exactly match the prior snapshot")
        attempt_iso = utc_iso(attempted_at)
        for index, row in enumerate(candidate_listings):
            if row.get("data_quality") != "confirmed":
                raise ValueError(f"successful row {index} is not confirmed")
            if utc_iso(row.get("retrieved")) != attempt_iso:
                raise ValueError(f"successful row {index} is not from the exact current attempt")
            _require_finite_positive(row.get("current_price"), f"successful row {index} current_price")
            if row.get("list_price") not in (None, ""):
                _require_finite_positive(row.get("list_price"), f"successful row {index} list_price")
        updated = copy.deepcopy(status)
        updated.update(
            {
                "data_refreshed_at_utc": attempt_iso,
                "last_attempt_at_utc": attempt_iso,
                "last_attempt_status": "success",
                "last_attempt_reason": None,
                "source_count": expected_count,
                "row_count": len(candidate_listings),
                "quality_counts": {
                    "verified": len(candidate_listings),
                    "estimated": 0,
                    "blocked": 0,
                },
                "rendered_at_utc": None,
                "published_at_utc": None,
            }
        )
        return copy.deepcopy(candidate_listings), validate_refresh_status(updated), True

    reason = str(outcome.get("reason") or "Refresh attempt did not complete.").strip()
    if not reason:
        raise ValueError(f"{outcome_status} refresh outcome requires a reason")
    updated = copy.deepcopy(status)
    updated.update(
        {
            "last_attempt_at_utc": utc_iso(attempted_at),
            "last_attempt_status": outcome_status,
            "last_attempt_reason": reason,
        }
    )
    return copy.deepcopy(prior_listings), validate_refresh_status(updated), False


def _stable_identity_set(listings: list[dict], label: str) -> frozenset[tuple[str, str, str, str]]:
    identities: list[tuple[str, str, str, str]] = []
    for index, row in enumerate(listings):
        if not isinstance(row, dict):
            raise ValueError(f"{label} listing {index} must be an object")
        identity = tuple(
            str(row.get(field) or "").strip()
            for field in ("brand", "model", "retailer", "source_url")
        )
        if not all(identity):
            raise ValueError(f"{label} listing {index} has an incomplete stable identity")
        identities.append(identity)
    if len(set(identities)) != len(identities):
        raise ValueError(f"{label} listings contain a duplicate stable identity")
    return frozenset(identities)


def _require_finite_positive(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive price")
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite positive price") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError(f"{label} must be a finite positive price")
    return amount
