"""Read-only MCP-style wrapper for Tier-4 metric computation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


ALLOWED_INPUT_KEYS = {"run_manifest_path"}
METRIC_NAME = "log_final_step_v3"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_result() -> dict[str, Any]:
    return {
        "metric_name": METRIC_NAME,
        "scalar": None,
        "details": {},
    }


def _parse_payload(payload: dict[str, Any]) -> Path:
    unknown_keys = sorted(set(payload.keys()) - ALLOWED_INPUT_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown payload keys: {unknown_keys}")

    raw = payload.get("run_manifest_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("run_manifest_path must be a non-empty string")

    manifest_path = Path(raw.strip()).expanduser()
    repo_root = _repo_root()
    if not manifest_path.is_absolute():
        manifest_path = (repo_root / manifest_path).resolve()
    else:
        manifest_path = manifest_path.resolve()
    return manifest_path


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("run manifest must be a JSON object")
    return payload


def _resolve_run_dir(manifest: dict[str, Any]) -> Path:
    raw = manifest.get("run_dir")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("manifest missing run_dir")

    run_dir = Path(raw.strip())
    if run_dir.is_absolute():
        return run_dir.resolve()

    repo_root = _repo_root()
    return (repo_root / run_dir).resolve()


def _resolve_preferred_log_path(manifest: dict[str, Any], manifest_path: Path, run_dir: Path) -> Path:
    raw = manifest.get("run_log")
    candidates: list[Path] = []
    if isinstance(raw, str) and raw.strip():
        run_log = Path(raw.strip())
        if run_log.is_absolute():
            candidates.append(run_log.resolve())
        else:
            candidates.append((run_dir / run_log).resolve())
            candidates.append((manifest_path.parent / run_log).resolve())
            candidates.append((_repo_root() / run_log).resolve())
    candidates.append((run_dir / "run.log").resolve())

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _parse_metric_stdout(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("metric runner produced no JSON payload")


def _run_metric_cli(run_dir: Path, manifest_path: Path) -> tuple[int, str, str]:
    repo_root = _repo_root()
    cmd = [
        "python3",
        "agent/metric_v3.py",
        "--run-dir",
        str(run_dir),
        "--manifest",
        str(manifest_path),
    ]
    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    return (proc.returncode, proc.stdout, proc.stderr)


def compute_metric(payload: dict[str, Any]) -> dict[str, Any]:
    """Compute metric from run manifest path without mutating filesystem state."""

    result = _default_result()

    try:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        manifest_path = _parse_payload(payload)
        if not manifest_path.exists():
            raise FileNotFoundError(f"run_manifest_path not found: {manifest_path}")

        manifest = _load_manifest(manifest_path)
        run_dir = _resolve_run_dir(manifest)
        preferred_log_path = _resolve_preferred_log_path(
            manifest=manifest,
            manifest_path=manifest_path,
            run_dir=run_dir,
        )

        exit_code, stdout, stderr = _run_metric_cli(run_dir=run_dir, manifest_path=manifest_path)
        details: dict[str, Any] = {
            "status": "success" if exit_code == 0 else "failed",
            "run_manifest_path": str(manifest_path),
            "run_dir": str(run_dir),
            "preferred_log_path": str(preferred_log_path),
            "manifest_run_log": manifest.get("run_log", ""),
            "runner": "python3 agent/metric_v3.py",
            "runner_exit_code": exit_code,
        }

        if exit_code != 0:
            error_msg = stderr.strip()
            if not error_msg:
                # metric_v3 prints METRIC_V3_ERROR to stdout on failure.
                error_msg = stdout.strip().splitlines()[-1] if stdout.strip() else "metric runner failed"
            details["error"] = error_msg
            result["details"] = details
            return result

        metric_payload = _parse_metric_stdout(stdout)
        scalar_raw = metric_payload.get("scalar", metric_payload.get("metric_value"))
        scalar: float | int | None = None
        if isinstance(scalar_raw, (int, float)) and not isinstance(scalar_raw, bool):
            scalar = scalar_raw

        details.update(
            {
                "parsed_field": metric_payload.get("parsed_field", ""),
                "log_path": metric_payload.get("log_path", ""),
                "source": metric_payload.get("source", ""),
                "snapshot_path": metric_payload.get("snapshot_path", ""),
                "fallback_metric_name": metric_payload.get("fallback_metric_name", ""),
            }
        )

        metric_name = metric_payload.get("metric_name")
        if not isinstance(metric_name, str) or not metric_name:
            metric_name = METRIC_NAME

        result["metric_name"] = metric_name
        result["scalar"] = scalar
        result["details"] = details
        return result
    except Exception as exc:  # noqa: BLE001
        result["details"] = {"status": "failed", "error": str(exc)}
        return result


__all__ = ["compute_metric"]
