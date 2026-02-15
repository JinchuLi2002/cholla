#!/usr/bin/env python3
"""Compute a deterministic, cheap Tier-4 metric per iteration.

Phase-1 metric choice:
- metric_name: snapshot_file_count
- metric_value: number of snapshot files under backend_run_dir

Snapshot files are identified by filename suffixes:
- *.h5
- *.h5.<rank>
- *.hdf5
- *.hdf5.<rank>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SNAPSHOT_FILE_RE = re.compile(r".*\.(?:h5|hdf5)(?:\.\d+)?$", re.IGNORECASE)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}")
    return payload


def _list_snapshot_files(run_dir: Path) -> list[Path]:
    if not run_dir.exists() or not run_dir.is_dir():
        return []
    matches = [p for p in run_dir.rglob("*") if p.is_file() and SNAPSHOT_FILE_RE.fullmatch(p.name)]
    return sorted(matches)


def compute_metric(execution_record: dict[str, Any]) -> dict[str, Any]:
    paths = execution_record.get("paths", {})
    if not isinstance(paths, dict):
        paths = {}

    run_dir_raw = paths.get("backend_run_dir", "")
    run_dir = Path(run_dir_raw) if isinstance(run_dir_raw, str) and run_dir_raw else Path("")

    snapshot_files = _list_snapshot_files(run_dir)
    metric_value = len(snapshot_files)

    rel_files: list[str] = []
    if run_dir and run_dir.exists():
        for p in snapshot_files:
            try:
                rel_files.append(str(p.relative_to(run_dir)))
            except ValueError:
                rel_files.append(str(p))

    return {
        "metric_name": "snapshot_file_count",
        "metric_value": metric_value,
        "iteration": execution_record.get("iteration"),
        "agent_run_id": execution_record.get("agent_run_id"),
        "backend_run_dir": str(run_dir) if str(run_dir) else "",
        "snapshot_files": rel_files,
    }


def _default_output_path(execution_record_path: Path, execution_record: dict[str, Any]) -> Path:
    paths = execution_record.get("paths", {})
    if isinstance(paths, dict):
        iteration_dir = paths.get("iteration_dir")
        if isinstance(iteration_dir, str) and iteration_dir:
            return Path(iteration_dir) / "metric.json"
    return execution_record_path.parent / "metric.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute deterministic Tier-4 per-iteration metric.")
    parser.add_argument(
        "--execution-record",
        type=Path,
        required=True,
        help="Path to iteration execution_record.json.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output metric JSON path. Default: <iteration_dir>/metric.json.",
    )
    args = parser.parse_args()

    execution_record_path = args.execution_record.resolve()
    execution_record = _load_json(execution_record_path)
    metric_payload = compute_metric(execution_record)

    out_path = args.out.resolve() if args.out else _default_output_path(execution_record_path, execution_record)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(metric_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(metric_payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
