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
DENSITY_EXACT_DATASETS = ("density", "rho")
VELOCITY_COMPONENT_ALIASES = (
    ("vx", "vel_x"),
    ("vy", "vel_y"),
    ("vz", "vel_z"),
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
    glob_candidates = _glob_snapshot_candidates(
        run_dir,
        ("**/*.h5.*", "**/*.h5", "**/*.hdf5.*", "**/*.hdf5"),
    )

    candidates = _ordered_unique(manifest_candidates + glob_candidates)
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda p: (p.name.lower(), str(p)))
    return ordered[-1]


def _dataset_basename(dataset_name: str) -> str:
    stripped = dataset_name.lstrip("/")
    return stripped.rsplit("/", 1)[-1].lower()


def _pick_density_dataset(dataset_names: list[str]) -> str | None:
    exact = sorted(name for name in dataset_names if _dataset_basename(name) in DENSITY_EXACT_DATASETS)
    if exact:
        return exact[0]

    containing_density = sorted(
        name
        for name in dataset_names
        if "density" in _dataset_basename(name) or "density" in name.lower()
    )
    if containing_density:
        return containing_density[0]
    return None


def _pick_velocity_datasets(dataset_names: list[str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for aliases in VELOCITY_COMPONENT_ALIASES:
        key = aliases[0]
        candidates = sorted(name for name in dataset_names if _dataset_basename(name) in aliases)
        if not candidates:
            return {}
        selected[key] = candidates[0]
    return selected


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

        rho_values = np.asarray(h5f[dataset_name][...], dtype=np.float64)
        mass = float(rho_values.sum())
        rho_var = float(rho_values.var())

        if not np.isfinite(mass):
            raise RuntimeError(f"metric_v2: non-finite density mass sum in {snapshot_path}")
        if mass == 0.0:
            raise RuntimeError(
                "metric_v2: density mass sum is zero; refusing trivial metric "
                f"(snapshot={snapshot_path}, dataset=/{dataset_name.lstrip('/')})"
            )
        if not np.isfinite(rho_var):
            raise RuntimeError(f"metric_v2: non-finite density variance in {snapshot_path}")

        velocity_datasets = _pick_velocity_datasets(dataset_names)
        v_mean: float | None = None
        velocity_used = False
        if velocity_datasets:
            vx_values = np.asarray(h5f[velocity_datasets["vx"]][...], dtype=np.float64)
            vy_values = np.asarray(h5f[velocity_datasets["vy"]][...], dtype=np.float64)
            vz_values = np.asarray(h5f[velocity_datasets["vz"]][...], dtype=np.float64)

            if vx_values.shape == vy_values.shape == vz_values.shape:
                speeds = np.sqrt(vx_values * vx_values + vy_values * vy_values + vz_values * vz_values)
                v_mean = float(speeds.mean())
                if not np.isfinite(v_mean):
                    raise RuntimeError(f"metric_v2: non-finite velocity mean in {snapshot_path}")
                velocity_used = True
            else:
                velocity_datasets = {}

        scalar = float(v_mean if velocity_used and v_mean is not None else rho_var)

    return {
        "metric_name": "density_mean_var_v2",
        "scalar": scalar,
        "metric_value": scalar,
        "mass": mass,
        "rho_var": rho_var,
        "v_mean": v_mean,
        "velocity_used": velocity_used,
        "source": "hdf5",
        "snapshot_path": str(snapshot_path),
        "density_dataset_path": f"/{dataset_name.lstrip('/')}",
        "velocity_dataset_paths": (
            {axis: f"/{path.lstrip('/')}" for axis, path in velocity_datasets.items()}
            if velocity_datasets
            else {}
        ),
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
            "mass": None,
            "rho_var": None,
            "v_mean": None,
            "velocity_used": False,
            "source": "log",
            "snapshot_path": "",
            "density_dataset_path": "",
            "velocity_dataset_paths": {},
            "log_path": str(run_log),
            "log_signal": "Current_z",
        }

    n_step = _extract_last_value(text, N_STEP_REGEXES, int)
    if isinstance(n_step, int):
        return {
            "metric_name": "density_mean_var_v2",
            "scalar": float(n_step),
            "metric_value": float(n_step),
            "mass": None,
            "rho_var": None,
            "v_mean": None,
            "velocity_used": False,
            "source": "log",
            "snapshot_path": "",
            "density_dataset_path": "",
            "velocity_dataset_paths": {},
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
    1) Last snapshot HDF5 file under run_dir (or manifest produced_files), sorted
       by filename.
       - Always compute: density mass sum and density variance.
       - If velocity components exist, scalar is mean(|v|); otherwise scalar is
         density variance.
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
