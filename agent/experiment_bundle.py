#!/usr/bin/env python3
"""Utilities for writing replayable Tier-4 experiment bundles."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _history_text(records: list[dict[str, Any]]) -> str:
    lines = [json.dumps(record, sort_keys=True) for record in records]
    return ("\n".join(lines) + "\n") if lines else ""


def _git_sha(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        value = proc.stdout.strip()
        if value:
            return value
    return "unknown"


def _best_from_history(records: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    best_record: dict[str, Any] | None = None
    best_scalar: float | None = None

    for record in records:
        if record.get("record_type") != "iteration":
            continue
        if record.get("status") != "success":
            continue

        metric = record.get("metric", {})
        if not isinstance(metric, dict):
            continue
        scalar_value = metric.get("scalar")
        if scalar_value is None:
            # Backward-compatibility for older bundles that only stored `value`.
            scalar_value = metric.get("value")
        if not isinstance(scalar_value, (int, float)) or isinstance(scalar_value, bool):
            continue

        scalar = float(scalar_value)
        if best_scalar is None or scalar > best_scalar:
            best_scalar = scalar
            best_record = record

    if best_record is None or best_scalar is None:
        return (None, None)

    metric = best_record.get("metric", {})
    metric_name = metric.get("name") if isinstance(metric, dict) else None
    best_metric = {
        "objective": "maximize_scalar",
        "selection_rule": "max_scalar_tie_breaker=earliest_iteration",
        "metric_name": metric_name if isinstance(metric_name, str) else "density_mean_var_v2",
        "scalar": best_scalar,
        "iteration": best_record.get("iteration"),
    }
    best_params = best_record.get("params")
    if not isinstance(best_params, dict):
        best_params = None
    return (best_metric, best_params)


def derive_experiment_id(seed: int, spec_path: Path, args_for_hash: dict[str, Any]) -> str:
    """Build deterministic experiment id: exp_<seed>_<short_hash>."""
    hasher = hashlib.sha256()
    hasher.update(spec_path.read_bytes())
    hasher.update(b"\n")
    hasher.update(json.dumps(args_for_hash, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    short_hash = hasher.hexdigest()[:12]
    return f"exp_{seed}_{short_hash}"


def write_experiment_bundle(
    *,
    repo_root: Path,
    experiment_id: str,
    config: dict[str, Any],
    spec_path: Path,
    template_params_path: Path | None = None,
    template_schedule_path: Path | None = None,
    seed: int,
    history_records: list[dict[str, Any]],
    history_source_path: Path | None = None,
    total_iterations_attempted: int,
    iters_succeeded: int,
    iters_failed: int,
    termination_reason: str,
) -> Path:
    """Write a replayable experiment bundle under agent/experiments/<experiment_id>/."""

    bundle_dir = repo_root / "agent" / "experiments" / experiment_id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    (bundle_dir / "config.json").write_text(_json_text(config), encoding="utf-8")
    shutil.copy2(spec_path, bundle_dir / "param_space.yaml")
    if template_params_path is not None and template_params_path.exists() and template_params_path.is_file():
        shutil.copy2(template_params_path, bundle_dir / "template_params.txt")
    if template_schedule_path is not None and template_schedule_path.exists() and template_schedule_path.is_file():
        shutil.copy2(template_schedule_path, bundle_dir / "template_schedule.txt")
    (bundle_dir / "seed.txt").write_text(f"{seed}\n", encoding="utf-8")
    # Keep a run-scoped history file as self-contained replay source.
    (bundle_dir / "history_experiment.jsonl").write_text(_history_text(history_records), encoding="utf-8")
    # Also copy the configured history.jsonl for provenance continuity if available.
    if history_source_path is not None and history_source_path.exists() and history_source_path.is_file():
        shutil.copy2(history_source_path, bundle_dir / "history.jsonl")
    else:
        (bundle_dir / "history.jsonl").write_text(_history_text(history_records), encoding="utf-8")

    best_metric, best_params = _best_from_history(history_records)
    summary = {
        "experiment_id": experiment_id,
        "total_iterations_attempted": total_iterations_attempted,
        "iters_succeeded": iters_succeeded,
        "iters_failed": iters_failed,
        "best_metric": best_metric,
        "best_params": best_params,
        "termination_reason": termination_reason,
        "git_sha": _git_sha(repo_root),
        "bundle_files": {
            "config": "config.json",
            "param_space": "param_space.yaml",
            "template_params": "template_params.txt",
            "template_schedule": "template_schedule.txt",
            "seed": "seed.txt",
            "history": "history.jsonl",
            "history_experiment": "history_experiment.jsonl",
            "summary": "summary.json",
        },
    }
    (bundle_dir / "summary.json").write_text(_json_text(summary), encoding="utf-8")
    return bundle_dir
