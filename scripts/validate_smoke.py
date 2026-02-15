#!/usr/bin/env python3
"""Validate smoke-run artifacts for the cosmology smoke test."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

# Adjustable pattern lists.
# FIRST_SNAPSHOT_PATTERNS are intentionally strict so deleting the first output
# file causes validation failure.
FIRST_SNAPSHOT_PATTERNS = [
    "0/0.h5.0",
    "**/0.h5.0",
]

# DATA_FILE_PATTERNS are broader and used to confirm there is at least one data
# output file.
DATA_FILE_PATTERNS = [
    "**/*.h5.*",
]

EXCLUDED_BASENAMES = {
    "run.log",
    "validator.log",
    "params.txt",
    "params.run.txt",
    "scale_outputs.txt",
    "README.snapshot.txt",
}


def collect_files(run_dir: Path, patterns: list[str]) -> list[Path]:
    """Collect unique files under run_dir matching any pattern."""
    seen: set[Path] = set()
    for pattern in patterns:
        for path in run_dir.glob(pattern):
            if not path.is_file():
                continue
            if path.name in EXCLUDED_BASENAMES:
                continue
            seen.add(path)
    return sorted(seen)


def rel_or_abs(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def validate(run_dir: Path, repo_root: Path) -> tuple[bool, dict]:
    missing: list[str] = []

    run_log = run_dir / "run.log"
    if not run_dir.exists() or not run_dir.is_dir():
        missing.append(f"run_dir missing or not a directory: {run_dir}")
    if not run_log.exists():
        missing.append(f"missing run.log: {run_log}")
    elif run_log.stat().st_size == 0:
        missing.append(f"empty run.log: {run_log}")

    first_snapshot_files: list[Path] = []
    data_files: list[Path] = []

    if run_dir.exists() and run_dir.is_dir():
        first_snapshot_files = collect_files(run_dir, FIRST_SNAPSHOT_PATTERNS)
        if not first_snapshot_files:
            missing.append(
                "missing first snapshot data file "
                f"(expected one of FIRST_SNAPSHOT_PATTERNS under {run_dir})"
            )
        else:
            for path in first_snapshot_files:
                if path.stat().st_size == 0:
                    missing.append(f"empty first snapshot data file: {path}")

        data_files = collect_files(run_dir, DATA_FILE_PATTERNS)
        if not data_files:
            missing.append(
                "no output data files found "
                f"(checked DATA_FILE_PATTERNS under {run_dir})"
            )
        else:
            for path in data_files:
                if path.stat().st_size == 0:
                    missing.append(f"empty output data file: {path}")

    status = "pass" if not missing else "fail"

    payload = {
        "run_dir": str(run_dir),
        "status": status,
        "missing": missing,
        "checked_patterns": {
            "first_snapshot_patterns": FIRST_SNAPSHOT_PATTERNS,
            "data_file_patterns": DATA_FILE_PATTERNS,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_log": str(run_log),
        "found": {
            "first_snapshot_files": [rel_or_abs(p, repo_root) for p in first_snapshot_files],
            "data_files": [rel_or_abs(p, repo_root) for p in data_files],
        },
    }
    return (status == "pass"), payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate cosmology smoke outputs.")
    parser.add_argument("--run_dir", required=True, help="Run directory to validate.")
    parser.add_argument(
        "--out",
        default="",
        help="Optional output JSON path. Defaults to artifacts/validation.json under repo root.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (Path.cwd() / run_dir).resolve()

    ok, payload = validate(run_dir=run_dir, repo_root=repo_root)

    if args.out:
        validation_path = Path(args.out)
        if not validation_path.is_absolute():
            validation_path = (Path.cwd() / validation_path).resolve()
    else:
        artifacts_dir = repo_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        validation_path = artifacts_dir / "validation.json"

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    if ok:
        print(f"Validation passed for {run_dir}")
        return 0

    print(f"Validation failed for {run_dir}", file=sys.stderr)
    for msg in payload["missing"]:
        print(f"- {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
