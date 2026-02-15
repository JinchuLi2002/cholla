#!/usr/bin/env python3
"""Compute a deterministic Tier-4 metric with log-first extraction (v3)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from metric_v2 import (
    _find_snapshot as _v2_find_snapshot,
    _load_json_if_exists as _v2_load_json_if_exists,
    _metric_from_hdf5 as _v2_metric_from_hdf5,
    _resolve_run_dir as _v2_resolve_run_dir,
)


METRIC_NAME = "log_final_step_v3"
FLOAT_TOKEN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
INT_TOKEN = r"[+-]?\d+(?!\.\d)"

STEP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("timestep", re.compile(rf"\btime[_\-\s]*steps?\b\s*(?:=|:)?\s*({INT_TOKEN})\b", re.IGNORECASE)),
    ("n_step", re.compile(rf"\bn[_\-\s]*steps?\b\s*(?:=|:)?\s*({INT_TOKEN})\b", re.IGNORECASE)),
    ("step", re.compile(rf"\bstep\b\s*(?:=|:)?\s*({INT_TOKEN})\b", re.IGNORECASE)),
)

STATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a", re.compile(rf"\ba\s*(?:=|:)\s*({FLOAT_TOKEN})\b", re.IGNORECASE)),
    ("z", re.compile(rf"\bz\s*(?:=|:)\s*({FLOAT_TOKEN})\b", re.IGNORECASE)),
    ("t", re.compile(rf"\bt\s*(?:=|:)\s*({FLOAT_TOKEN})\b", re.IGNORECASE)),
)


def _ordered_unique(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _manifest_log_candidates(run_dir: Path, manifest: dict[str, Any], manifest_path: Path | None) -> list[Path]:
    candidates: list[Path] = []

    run_log = manifest.get("run_log")
    if isinstance(run_log, str) and run_log.strip():
        run_log_path = Path(run_log.strip())
        if run_log_path.is_absolute():
            candidates.append(run_log_path)
        else:
            candidates.append((run_dir / run_log_path).resolve())
            if manifest_path is not None:
                candidates.append((manifest_path.parent / run_log_path).resolve())
            candidates.append(run_log_path.resolve())

    produced_files = manifest.get("produced_files")
    if isinstance(produced_files, list):
        for entry in produced_files:
            if not isinstance(entry, str) or not entry.strip():
                continue
            candidate = Path(entry.strip())
            if not candidate.is_absolute():
                candidate = (run_dir / candidate).resolve()
            if candidate.suffix.lower() != ".log":
                continue
            if "run" not in candidate.name.lower():
                continue
            candidates.append(candidate)

    return candidates


def _search_log_candidates(run_dir: Path) -> list[Path]:
    candidates: list[Path] = []

    candidates.append((run_dir / "run.log").resolve())

    for path in sorted(run_dir.glob("*.log"), key=lambda p: p.name.lower()):
        if "run" in path.name.lower():
            candidates.append(path.resolve())

    for path in sorted(run_dir.rglob("*.log"), key=lambda p: str(p).lower()):
        if "run" in path.name.lower():
            candidates.append(path.resolve())

    return candidates


def _resolve_run_log_path(run_dir: Path, manifest: dict[str, Any], manifest_path: Path | None) -> Path | None:
    candidates = _ordered_unique(
        _manifest_log_candidates(run_dir, manifest, manifest_path) + _search_log_candidates(run_dir)
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _extract_last_match(
    text: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
    caster: type[int] | type[float],
) -> tuple[str, int | float] | None:
    last_match: tuple[int, str, int | float] | None = None
    for field_name, pattern in patterns:
        for match in pattern.finditer(text):
            try:
                value = caster(match.group(1))
            except ValueError:
                continue
            position = match.start(1)
            if last_match is None or position > last_match[0]:
                last_match = (position, field_name, value)
    if last_match is None:
        return None
    return (last_match[1], last_match[2])


def _parse_scalar_from_log(run_log: Path) -> tuple[str, int | float] | None:
    text = run_log.read_text(encoding="utf-8", errors="replace")

    step_value = _extract_last_match(text, STEP_PATTERNS, int)
    if step_value is not None:
        return step_value

    state_value = _extract_last_match(text, STATE_PATTERNS, float)
    if state_value is not None:
        return state_value

    return None


def _metric_from_log(run_log: Path) -> dict[str, Any] | None:
    parsed = _parse_scalar_from_log(run_log)
    if parsed is None:
        return None

    parsed_field, scalar = parsed
    return {
        "metric_name": METRIC_NAME,
        "scalar": scalar,
        "metric_value": scalar,
        "source": "log",
        "log_path": str(run_log),
        "parsed_field": parsed_field,
        "snapshot_path": "",
    }


def _metric_from_hdf5_fallback(run_dir: Path, manifest: dict[str, Any], run_log: Path | None) -> dict[str, Any]:
    snapshot_path = _v2_find_snapshot(run_dir, manifest)
    if snapshot_path is None:
        raise RuntimeError(
            "metric_v3: log parsing failed and no HDF5 snapshot found "
            f"(run_dir={run_dir}, run_log={run_log})"
        )

    payload = dict(_v2_metric_from_hdf5(snapshot_path))
    scalar_raw = payload.get("scalar", payload.get("metric_value"))
    if not isinstance(scalar_raw, (int, float)) or isinstance(scalar_raw, bool):
        raise RuntimeError(f"metric_v3: HDF5 fallback produced non-numeric scalar ({scalar_raw!r})")

    scalar = float(scalar_raw)
    payload["metric_name"] = METRIC_NAME
    payload["scalar"] = scalar
    payload["metric_value"] = scalar
    payload["source"] = "hdf5"
    payload["parsed_field"] = "hdf5_fallback"
    payload["log_path"] = str(run_log) if run_log is not None else ""
    payload["fallback_metric_name"] = "density_mean_var_v2"
    return payload


def compute_metric(run_dir: str, manifest_path: str | None = None) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    manifest_path_obj: Path | None = None
    if manifest_path is not None and manifest_path.strip():
        manifest_path_obj = Path(manifest_path.strip())
        manifest = _v2_load_json_if_exists(manifest_path_obj)

    run_dir_path = _v2_resolve_run_dir(run_dir, manifest)
    run_log = _resolve_run_log_path(run_dir_path, manifest, manifest_path_obj)

    if run_log is not None:
        log_metric = _metric_from_log(run_log)
        if log_metric is not None:
            return log_metric

    return _metric_from_hdf5_fallback(run_dir_path, manifest, run_log)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute Tier-4 v3 metric from run outputs.")
    parser.add_argument("--run-dir", type=str, required=True, help="Backend run directory.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Optional run_manifest.json path.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output JSON path.",
    )
    args = parser.parse_args()

    try:
        payload = compute_metric(
            run_dir=args.run_dir,
            manifest_path=str(args.manifest.resolve()) if args.manifest else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"METRIC_V3_ERROR: {exc}")
        return 1

    if args.out:
        out_path = args.out.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
