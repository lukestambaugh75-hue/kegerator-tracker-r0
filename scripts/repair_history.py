#!/usr/bin/env python3
"""Remove false estimated Kegerator history while preserving exact CSV bytes."""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "history.csv"
HEADER = "date,brand,model,retailer,price,list_price,source,data_quality\n"
FIELDS = HEADER.rstrip("\n").split(",")
KNOWN_QUALITIES = {"confirmed", "estimated"}


def _validate_row(raw_line: str, line_number: int) -> list[str]:
    try:
        rows = list(csv.reader([raw_line], strict=True))
    except csv.Error as exc:
        raise ValueError(f"malformed CSV row at line {line_number}: {exc}") from exc
    if len(rows) != 1 or len(rows[0]) != len(FIELDS):
        raise ValueError(f"malformed CSV row at line {line_number}")
    values = rows[0]
    row = dict(zip(FIELDS, values))
    try:
        date.fromisoformat(row["date"])
    except ValueError as exc:
        raise ValueError(f"invalid date at line {line_number}: {row['date']}") from exc
    for field in ("brand", "model", "retailer"):
        if not row[field].strip():
            raise ValueError(f"missing {field} at line {line_number}")
    try:
        price = float(row["price"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError
        if row["list_price"]:
            list_price = float(row["list_price"])
            if not math.isfinite(list_price) or list_price <= 0:
                raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid price at line {line_number}") from exc
    parsed = urlsplit(row["source"])
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"invalid source URL at line {line_number}")
    quality = row["data_quality"]
    if quality not in KNOWN_QUALITIES:
        raise ValueError(f"unknown data_quality at line {line_number}: {quality}")
    return values


def repair_history(path: Path = DEFAULT_PATH, check: bool = False) -> tuple[int, int]:
    """Return kept/removed counts and atomically repair unless ``check`` is true."""
    path = Path(path)
    raw = path.read_bytes()
    if b"\r" in raw:
        raise ValueError("history.csv must use LF line endings")
    if not raw.endswith(b"\n"):
        raise ValueError("history.csv must end with a trailing newline")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("history.csv must be valid UTF-8") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0] != HEADER:
        raise ValueError("history.csv header does not match the exact required header")

    kept_lines: list[str] = []
    removed = 0
    for line_number, raw_line in enumerate(lines[1:], start=2):
        if not raw_line.strip():
            raise ValueError(f"blank history row at line {line_number}")
        values = _validate_row(raw_line, line_number)
        if values[-1] == "confirmed":
            kept_lines.append(raw_line)
        else:
            removed += 1

    if not check and removed:
        repaired = (HEADER + "".join(kept_lines)).encode("utf-8")
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(repaired)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
    return len(kept_lines), removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        kept, removed = repair_history(args.path, check=args.check)
    except Exception as exc:
        print(f"history repair failed: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(f"{kept} kept, {removed} would remove")
        return 1 if removed else 0
    print(f"{kept} kept, {removed} removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
