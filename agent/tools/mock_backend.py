"""Deterministic CPU-safe mock backend for Phase 3D wiring and replay smoke tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ALLOWED_RUN_INPUT_KEYS = {"params_text", "schedule_text", "out_root", "run_id"}
ALLOWED_METRIC_INPUT_KEYS = {"run_manifest_path"}
MOCK_METRIC_NAME = "mock_density_scalar_v1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_path(path_text: str, *, repo_root: Path) -> Path:
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()


def _parse_run_payload(payload: dict[str, Any]) -> tuple[str, str, Path]:
    unknown_keys = sorted(set(payload.keys()) - ALLOWED_RUN_INPUT_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown payload keys: {unknown_keys}")

    params_text = payload.get("params_text")
    schedule_text = payload.get("schedule_text")
    out_root_raw = payload.get("out_root")

    if not isinstance(params_text, str):
        raise ValueError("params_text must be a string")
    if not isinstance(schedule_text, str):
        raise ValueError("schedule_text must be a string")
    if out_root_raw is not None and not isinstance(out_root_raw, str):
        raise ValueError("out_root must be a string when provided")

    repo_root = _repo_root()
    if isinstance(out_root_raw, str) and out_root_raw.strip():
        out_root = _resolve_path(out_root_raw.strip(), repo_root=repo_root)
    else:
        out_root = (repo_root / "agent" / "tools" / "_mock_runs").resolve()
    return (params_text, schedule_text, out_root)


def _parse_metric_payload(payload: dict[str, Any]) -> Path:
    unknown_keys = sorted(set(payload.keys()) - ALLOWED_METRIC_INPUT_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown payload keys: {unknown_keys}")

    manifest_raw = payload.get("run_manifest_path")
    if not isinstance(manifest_raw, str) or not manifest_raw.strip():
        raise ValueError("run_manifest_path must be a non-empty string")
    return _resolve_path(manifest_raw.strip(), repo_root=_repo_root())


def _run_id_from_text(*, params_text: str, schedule_text: str) -> str:
    digest = hashlib.sha256(
        params_text.encode("utf-8") + b"\n<schedule>\n" + schedule_text.encode("utf-8")
    ).hexdigest()
    return f"mock_{digest[:12]}"


def _parse_param_value(raw_value: str) -> Any:
    value = raw_value.strip()
    if value == "":
        return ""
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." not in value and "e" not in lowered:
            return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _params_from_text(params_text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in params_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        out[key] = _parse_param_value(raw_value)
    return out


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_cholla(payload: dict[str, Any]) -> dict[str, Any]:
    """Materialize a deterministic synthetic run directory + manifest for CI-safe smoke."""

    result = {
        "status": "failed",
        "run_id": "",
        "run_dir": "",
        "run_manifest_path": "",
        "validation_path": "",
        "produced_files": [],
        "error": "",
    }

    try:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        params_text, schedule_text, out_root = _parse_run_payload(payload)
        run_id = _run_id_from_text(params_text=params_text, schedule_text=schedule_text)
        run_dir = (out_root / run_id).resolve()
        inputs_dir = run_dir / "inputs"
        artifacts_dir = run_dir / "artifacts"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        params_path = inputs_dir / "params.txt"
        schedule_path = inputs_dir / "scale_outputs.txt"
        params_path.write_text(params_text, encoding="utf-8")
        schedule_path.write_text(schedule_text, encoding="utf-8")

        parsed_params = _params_from_text(params_text)
        validation_payload = {
            "status": "pass",
            "validator": "mock_backend",
            "run_id": run_id,
        }
        validation_path = artifacts_dir / "validation.json"
        _json_write(validation_path, validation_payload)

        run_manifest_payload = {
            "status": "success",
            "tool_backend": "mock",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "run_log": "run.log",
            "params": parsed_params,
            "schedule_sha256": hashlib.sha256(schedule_text.encode("utf-8")).hexdigest(),
        }
        run_manifest_path = artifacts_dir / "run_manifest.tool.json"
        _json_write(run_manifest_path, run_manifest_payload)
        (run_dir / "run.log").write_text("mock backend: synthetic run\n", encoding="utf-8")

        produced_files: list[str] = []
        for path in sorted(run_dir.rglob("*"), key=lambda item: str(item)):
            if path.is_file():
                produced_files.append(str(path))

        result.update(
            {
                "status": "success",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "run_manifest_path": str(run_manifest_path),
                "validation_path": str(validation_path),
                "produced_files": produced_files,
                "error": "",
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = str(exc)
        return result


def compute_metric(payload: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic metric scalar from mock run-manifest params."""

    result = {
        "metric_name": MOCK_METRIC_NAME,
        "scalar": None,
        "details": {"status": "failed"},
    }

    try:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        manifest_path = _parse_metric_payload(payload)
        if not manifest_path.exists() or not manifest_path.is_file():
            raise FileNotFoundError(f"run_manifest_path not found: {manifest_path}")

        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest_payload, dict):
            raise ValueError("run manifest must be a JSON object")
        params = manifest_payload.get("params")
        params_map = dict(params) if isinstance(params, dict) else {}

        init_redshift_raw = params_map.get("Init_redshift", 0.0)
        if isinstance(init_redshift_raw, (int, float)) and not isinstance(init_redshift_raw, bool):
            init_redshift = float(init_redshift_raw)
        else:
            init_redshift = 0.0
        scalar = round(init_redshift, 6)

        result["scalar"] = scalar
        result["details"] = {
            "status": "success",
            "tool_backend": "mock",
            "run_manifest_path": str(manifest_path),
            "run_id": manifest_payload.get("run_id"),
            "formula": "scalar = round(Init_redshift, 6)",
        }
        return result
    except Exception as exc:  # noqa: BLE001
        result["details"] = {"status": "failed", "error": str(exc)}
        return result


__all__ = ["compute_metric", "run_cholla"]
