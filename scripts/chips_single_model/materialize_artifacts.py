#!/usr/bin/env python3
"""Materialize canonical artifacts for the CHIPS single-model baseline.

This script writes the following files under <run_dir>/artifacts/:
  - RunConfig.json
  - ics_manifest.json
  - sim_manifest.json
  - analysis_manifest.json
  - validation.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import h5py
except ImportError:  # pragma: no cover - import guard only
    h5py = None


H5_FILE_RE = re.compile(r".*\.h5(?:\.\d+)?$|.*\.hdf5$")
PRIMARY_SNAPSHOT_RE = re.compile(r"^\d+\.h5(?:\.\d+)?$")

REQUIRED_ANALYSIS_DATASETS = [
    "/lya_statistics/power_spectrum/k_vals",
    "/lya_statistics/power_spectrum/p(k)",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def read_text(path: Path) -> str:
    return path.read_text()


def parse_params(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def parse_schedule_values(path: Path) -> list[float]:
    values: list[float] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split()[0]
        values.append(float(token))
    return values


def maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if hasattr(value, "shape"):
            # Handles numpy/h5py scalar arrays and 1-element arrays.
            if getattr(value, "shape", ()) == ():
                value = value.item()
            elif len(value) > 0:
                value = value[0]
        if hasattr(value, "item"):
            value = value.item()
        return float(value)
    except Exception:
        return None


def canonical_z(z: float) -> str:
    return f"{z:.6f}"


def collect_h5_files(run_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file():
            continue
        if H5_FILE_RE.match(p.name):
            files.append(p)
    return files


def is_analysis_file(path: Path) -> bool:
    return path.name.endswith("_analysis.h5")


def is_skewers_file(path: Path) -> bool:
    return path.name.endswith("_skewers.h5")


def is_primary_snapshot_file(path: Path) -> bool:
    return PRIMARY_SNAPSHOT_RE.match(path.name) is not None


def file_entry(path: Path, run_dir: Path) -> dict[str, Any]:
    return {
        "path": rel(path, run_dir),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def extract_current_z_from_snapshot(path: Path) -> float | None:
    try:
        with h5py.File(path, "r") as h5f:
            return maybe_float(h5f.attrs.get("Current_z"))
    except Exception:
        return None


def extract_current_z_from_analysis_or_skewer(path: Path) -> float | None:
    try:
        with h5py.File(path, "r") as h5f:
            return maybe_float(h5f.attrs.get("current_z"))
    except Exception:
        return None


def collect_pk_datasets(analysis_file: Path, run_dir: Path) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    with h5py.File(analysis_file, "r") as h5f:
        for dset_path in REQUIRED_ANALYSIS_DATASETS:
            if dset_path not in h5f:
                continue
            dset = h5f[dset_path]
            data = dset[...]
            extracted.append(
                {
                    "file": rel(analysis_file, run_dir),
                    "dataset": dset_path,
                    "shape": list(dset.shape),
                    "dtype": str(dset.dtype),
                    "sha256": sha256_bytes(data.tobytes()),
                }
            )
    return extracted


def choose_snapshot_files_for_z_checks(snapshot_files: list[Path]) -> list[Path]:
    primaries = [p for p in snapshot_files if is_primary_snapshot_file(p)]
    if not primaries:
        return []

    rank0 = [p for p in primaries if p.name.endswith(".0")]
    if rank0:
        return rank0
    return primaries


def main() -> int:
    if h5py is None:
        raise SystemExit("h5py is required to run materialize_artifacts.py")

    parser = argparse.ArgumentParser(description="Materialize CHIPS single-model artifacts.")
    parser.add_argument("--run-dir", required=True, help="Run directory containing simulation outputs.")
    parser.add_argument("--params", required=True, help="Params file used for the run.")
    parser.add_argument("--schedule", required=True, help="Snapshot schedule file used for the run.")
    parser.add_argument("--git-sha", required=True, help="Git SHA used for the run.")
    parser.add_argument(
        "--analysis-schedule",
        default="",
        help="Optional analysis schedule file used for the run.",
    )
    parser.add_argument("--box-size", default="", help="Optional box size metadata.")
    parser.add_argument("--resolution", default="", help="Optional resolution metadata.")
    parser.add_argument("--z-init", default="", help="Optional initial redshift metadata.")
    parser.add_argument("--seed", default="", help="Optional seed metadata.")
    parser.add_argument(
        "--required-z",
        nargs="+",
        type=float,
        default=[5.0, 4.6, 4.2],
        help="Required snapshot redshifts for validation (default: 5.0 4.6 4.2).",
    )
    parser.add_argument(
        "--fail-on-validation",
        action="store_true",
        help="Exit non-zero when validation status is fail.",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    params_path = Path(args.params).resolve()
    schedule_path = Path(args.schedule).resolve()
    analysis_schedule_path = Path(args.analysis_schedule).resolve() if args.analysis_schedule else None

    if not run_dir.is_dir():
        raise SystemExit(f"run_dir does not exist or is not a directory: {run_dir}")
    if not params_path.is_file():
        raise SystemExit(f"params file not found: {params_path}")
    if not schedule_path.is_file():
        raise SystemExit(f"schedule file not found: {schedule_path}")
    if analysis_schedule_path is not None and not analysis_schedule_path.is_file():
        raise SystemExit(f"analysis schedule file not found: {analysis_schedule_path}")

    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    params_text = read_text(params_path)
    schedule_text = read_text(schedule_path)
    analysis_schedule_text = read_text(analysis_schedule_path) if analysis_schedule_path else ""

    params_sha = sha256_bytes(params_text.encode("utf-8"))
    schedule_sha = sha256_bytes(schedule_text.encode("utf-8"))
    analysis_schedule_sha = (
        sha256_bytes(analysis_schedule_text.encode("utf-8")) if analysis_schedule_path else ""
    )
    combined_inputs_sha = sha256_bytes(
        (
            params_text
            + "\n===SCHEDULE===\n"
            + schedule_text
            + "\n===ANALYSIS_SCHEDULE===\n"
            + analysis_schedule_text
        ).encode("utf-8")
    )

    params_map = parse_params(params_path)
    schedule_vals = parse_schedule_values(schedule_path)
    analysis_schedule_vals = parse_schedule_values(analysis_schedule_path) if analysis_schedule_path else []

    run_config = {
        "timestamp_utc": now_utc(),
        "git_sha": args.git_sha,
        "run_dir": str(run_dir),
        "inputs": {
            "params_path": str(params_path),
            "schedule_path": str(schedule_path),
            "analysis_schedule_path": (str(analysis_schedule_path) if analysis_schedule_path else ""),
            "params_sha256": params_sha,
            "schedule_sha256": schedule_sha,
            "analysis_schedule_sha256": analysis_schedule_sha,
            "combined_inputs_sha256": combined_inputs_sha,
        },
        "metadata": {
            "box_size": args.box_size,
            "resolution": args.resolution,
            "z_init": args.z_init,
            "seed": args.seed,
        },
        "required_snapshot_z": args.required_z,
        "params": params_map,
        "schedule_values": schedule_vals,
        "analysis_schedule_values": analysis_schedule_vals,
    }
    json_dump(artifacts_dir / "RunConfig.json", run_config)

    ics_manifest = {
        "timestamp_utc": now_utc(),
        "status": "success",
        "run_dir": str(run_dir),
        "ic_mode": "built_in",
        "seed": args.seed,
        "ic_files": [],
        "inputs": {
            "params_path": str(params_path),
            "schedule_path": str(schedule_path),
            "analysis_schedule_path": (str(analysis_schedule_path) if analysis_schedule_path else ""),
            "params_sha256": params_sha,
            "schedule_sha256": schedule_sha,
            "analysis_schedule_sha256": analysis_schedule_sha,
            "combined_inputs_sha256": combined_inputs_sha,
        },
    }
    json_dump(artifacts_dir / "ics_manifest.json", ics_manifest)

    all_h5_files = collect_h5_files(run_dir)
    analysis_files = [p for p in all_h5_files if is_analysis_file(p)]
    skewers_files = [p for p in all_h5_files if is_skewers_file(p)]
    snapshot_files = [p for p in all_h5_files if p not in analysis_files and p not in skewers_files]

    sim_snapshot_entries: list[dict[str, Any]] = []
    for p in snapshot_files:
        entry = file_entry(p, run_dir)
        current_z = extract_current_z_from_snapshot(p) if is_primary_snapshot_file(p) else None
        if current_z is not None:
            entry["Current_z"] = current_z
        sim_snapshot_entries.append(entry)

    sim_manifest = {
        "timestamp_utc": now_utc(),
        "status": "success",
        "run_dir": str(run_dir),
        "snapshot_file_count": len(sim_snapshot_entries),
        "snapshot_files": sim_snapshot_entries,
    }
    json_dump(artifacts_dir / "sim_manifest.json", sim_manifest)

    analysis_entries: list[dict[str, Any]] = []
    pk_datasets: list[dict[str, Any]] = []
    for p in analysis_files:
        entry = file_entry(p, run_dir)
        entry["current_z"] = extract_current_z_from_analysis_or_skewer(p)
        analysis_entries.append(entry)
        try:
            pk_datasets.extend(collect_pk_datasets(p, run_dir))
        except Exception as exc:
            analysis_entries[-1]["pk_extract_error"] = str(exc)

    skewers_entries: list[dict[str, Any]] = []
    for p in skewers_files:
        entry = file_entry(p, run_dir)
        entry["current_z"] = extract_current_z_from_analysis_or_skewer(p)
        skewers_entries.append(entry)

    analysis_manifest = {
        "timestamp_utc": now_utc(),
        "status": "success",
        "run_dir": str(run_dir),
        "required_dataset_paths": REQUIRED_ANALYSIS_DATASETS,
        "analysis_file_count": len(analysis_entries),
        "skewers_file_count": len(skewers_entries),
        "analysis_files": analysis_entries,
        "skewers_files": skewers_entries,
        "pk_datasets": pk_datasets,
    }
    json_dump(artifacts_dir / "analysis_manifest.json", analysis_manifest)

    snapshot_for_z = choose_snapshot_files_for_z_checks(snapshot_files)
    snapshot_z_entries: list[dict[str, Any]] = []
    actual_z_canonical: set[str] = set()
    for p in snapshot_for_z:
        z_val = extract_current_z_from_snapshot(p)
        if z_val is None:
            continue
        z_norm = canonical_z(z_val)
        actual_z_canonical.add(z_norm)
        snapshot_z_entries.append(
            {
                "file": rel(p, run_dir),
                "Current_z": z_val,
                "Current_z_canonical": z_norm,
            }
        )

    required_z_canonical = [canonical_z(z) for z in args.required_z]
    missing_required_z = [z for z in required_z_canonical if z not in actual_z_canonical]

    dataset_presence = {
        dset_path: any(item["dataset"] == dset_path for item in pk_datasets) for dset_path in REQUIRED_ANALYSIS_DATASETS
    }
    missing_analysis_datasets = [k for k, present in dataset_presence.items() if not present]

    validation_missing: list[str] = []
    if missing_required_z:
        validation_missing.append(
            "missing required snapshot Current_z values: " + ", ".join(missing_required_z)
        )
    if missing_analysis_datasets:
        validation_missing.append(
            "missing required analysis datasets: " + ", ".join(missing_analysis_datasets)
        )

    validation_status = "pass" if not validation_missing else "fail"
    validation_payload = {
        "timestamp_utc": now_utc(),
        "status": validation_status,
        "run_dir": str(run_dir),
        "required_snapshot_z": args.required_z,
        "required_snapshot_z_canonical": required_z_canonical,
        "observed_snapshot_z": snapshot_z_entries,
        "required_analysis_datasets": REQUIRED_ANALYSIS_DATASETS,
        "analysis_dataset_presence": dataset_presence,
        "missing": validation_missing,
    }
    json_dump(artifacts_dir / "validation.json", validation_payload)

    if args.fail_on_validation and validation_status != "pass":
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
