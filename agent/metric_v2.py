#!/usr/bin/env python3
"""Compute a deterministic Tier-4 metric from real simulation outputs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import numpy as np


SNAPSHOT_FILE_RE = re.compile(r".*\.(?:h5|hdf5)(?:\.\d+)?$", re.IGNORECASE)
COMMON_DENSITY_DATASETS = (
    "density",
    "gas_density",
    "d_density",
)
CURRENT_Z_REGEXES = (
    re.compile(r"Current_z\s*[:=]\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"),
    re.compile(r"current_z\s*[:=]\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", re.IGNORECASE),
)
N_STEP_REGEXES = (
    re.compile(r"\bn_step\s*[:=]\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bNstep\s*=\s*(\d+)", re.IGNORECASE),
)


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _resolve_run_dir(run_dir: str, manifest: dict[str, Any]) -> Path:
    candidate = run_dir.strip()
    if not candidate:
        from_manifest = manifest.get("run_dir")
        if isinstance(from_manifest, str) and from_manifest.strip():
            candidate = from_manifest.strip()
    if not candidate:
        raise RuntimeError("metric_v2: missing run_dir (both argument and manifest.run_dir are empty)")
    path = Path(candidate)
    if not path.is_absolute():
        path = path.resolve()
    return path


def _manifest_snapshot_candidates(run_dir: Path, manifest: dict[str, Any]) -> list[Path]:
    produced = manifest.get("produced_files")
    if not isinstance(produced, list):
        return []

    out: list[Path] = []
    for entry in produced:
        if not isinstance(entry, str) or not entry:
            continue
        path = Path(entry)
        if not path.is_absolute():
            path = (run_dir / path).resolve()
        if path.is_file() and SNAPSHOT_FILE_RE.fullmatch(path.name):
            out.append(path)
    return sorted(out, key=lambda p: str(p))


def _glob_snapshot_candidates(run_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for pattern in patterns:
        for path in run_dir.glob(pattern):
            if path.is_file() and SNAPSHOT_FILE_RE.fullmatch(path.name):
                out.append(path.resolve())
    return sorted(out, key=lambda p: str(p))


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


def _find_snapshot(run_dir: Path, manifest: dict[str, Any]) -> Path | None:
    manifest_candidates = _manifest_snapshot_candidates(run_dir, manifest)
    first_manifest = [p for p in manifest_candidates if p.name == "0.h5.0"]
    other_manifest = [p for p in manifest_candidates if p.name != "0.h5.0"]

    first_glob = _glob_snapshot_candidates(run_dir, ("0/0.h5.0", "**/0.h5.0"))
    other_glob = _glob_snapshot_candidates(
        run_dir,
        ("**/*.h5.*", "**/*.h5", "**/*.hdf5.*", "**/*.hdf5"),
    )

    candidates = _ordered_unique(first_manifest + first_glob + other_manifest + other_glob)
    if not candidates:
        return None
    return candidates[0]


def _pick_density_dataset(dataset_names: list[str]) -> str | None:
    normalized = {name.lstrip("/"): name for name in dataset_names}

    for key in COMMON_DENSITY_DATASETS:
        if key in normalized:
            return normalized[key]

    containing_density = sorted(name for name in dataset_names if "density" in name.lower())
    if containing_density:
        return containing_density[0]
    return None


def _extract_last_value(text: str, patterns: tuple[re.Pattern[str], ...], caster: type[float] | type[int]) -> float | int | None:
    last: tuple[int, float | int] | None = None
    for pattern in patterns:
        for match in pattern.finditer(text):
            try:
                value = caster(match.group(1))
            except ValueError:
                continue
            position = match.start(1)
            if last is None or position > last[0]:
                last = (position, value)
    return None if last is None else last[1]


def _resolve_run_log_path(run_dir: Path, manifest: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []

    from_manifest = manifest.get("run_log")
    if isinstance(from_manifest, str) and from_manifest.strip():
        raw = Path(from_manifest.strip())
        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.append((run_dir / raw).resolve())
            candidates.append(raw.resolve())

    candidates.append((run_dir / "run.log").resolve())

    for path in candidates:
        if path.is_file():
            return path
    return None


def _metric_from_hdf5(snapshot_path: Path) -> dict[str, Any]:
    try:
        import h5py
    except ModuleNotFoundError as exc:
        raise RuntimeError("metric_v2: h5py is required to read HDF5 snapshots") from exc

    with h5py.File(snapshot_path, "r") as h5f:
        dataset_names: list[str] = []

        def _visitor(name: str, obj: Any) -> None:
            if isinstance(obj, h5py.Dataset):
                dataset_names.append(name)

        h5f.visititems(_visitor)
        dataset_names = sorted(dataset_names)

        dataset_name = _pick_density_dataset(dataset_names)
        if dataset_name is None:
            raise RuntimeError(
                "metric_v2: no density-like dataset found in snapshot "
                f"{snapshot_path} (datasets={dataset_names})"
            )

        values = np.asarray(h5f[dataset_name][...], dtype=np.float64)
        mean = float(values.mean())
        var = float(values.var())
        scalar = float(mean + var)

    return {
        "metric_name": "density_mean_var_v2",
        "scalar": scalar,
        "metric_value": scalar,
        "mean": mean,
        "var": var,
        "source": "hdf5",
        "snapshot_path": str(snapshot_path),
        "dataset_path": f"/{dataset_name.lstrip('/')}",
    }


def _metric_from_log(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    run_log = _resolve_run_log_path(run_dir, manifest)
    if run_log is None:
        raise RuntimeError(
            "metric_v2: no HDF5 snapshot found and no run.log found for fallback "
            f"(run_dir={run_dir})"
        )

    text = run_log.read_text(encoding="utf-8", errors="replace")

    current_z = _extract_last_value(text, CURRENT_Z_REGEXES, float)
    if isinstance(current_z, float):
        return {
            "metric_name": "density_mean_var_v2",
            "scalar": float(current_z),
            "metric_value": float(current_z),
            "mean": None,
            "var": None,
            "source": "log",
            "snapshot_path": "",
            "log_path": str(run_log),
            "log_signal": "Current_z",
        }

    n_step = _extract_last_value(text, N_STEP_REGEXES, int)
    if isinstance(n_step, int):
        return {
            "metric_name": "density_mean_var_v2",
            "scalar": float(n_step),
            "metric_value": float(n_step),
            "mean": None,
            "var": None,
            "source": "log",
            "snapshot_path": "",
            "log_path": str(run_log),
            "log_signal": "n_step",
        }

    raise RuntimeError(
        "metric_v2: no HDF5 snapshot found and run.log does not contain parseable "
        f"Current_z or n_step values (run_log={run_log})"
    )


def compute_metric(run_dir: str, manifest_path: str | None = None) -> dict[str, Any]:
    """Compute deterministic scalar metric from run outputs.

    Preference order:
    1) First snapshot HDF5 file under run_dir (or manifest produced_files), using
       density data mean/variance.
    2) run.log fallback (final Current_z, then final n_step).
    """

    manifest = {}
    if manifest_path is not None and manifest_path.strip():
        manifest = _load_json_if_exists(Path(manifest_path.strip()))

    run_dir_path = _resolve_run_dir(run_dir, manifest)
    snapshot_path = _find_snapshot(run_dir_path, manifest)
    if snapshot_path is not None:
        return _metric_from_hdf5(snapshot_path)
    return _metric_from_log(run_dir_path, manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute Tier-4 v2 metric from run outputs.")
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
        print(f"METRIC_V2_ERROR: {exc}")
        return 1

    if args.out:
        out_path = args.out.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
