"""MCP-style wrapper for running scripts/colab_smoke.sh with JSON in/out."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_ID_RE = re.compile(r"(?m)^RUN_ID=([^\r\n]+)\s*$")
RUN_DIR_RE = re.compile(r"(?m)^RUN_DIR=([^\r\n]+)\s*$")
ALLOWED_INPUT_KEYS = {"params_text", "schedule_text", "out_root", "run_id"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_out_root(repo_root: Path, out_root: str | None, stage_dir: Path) -> Path:
    if out_root is None:
        return (stage_dir / "backend_runs").resolve()

    candidate = Path(out_root).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()


def _default_result() -> dict[str, Any]:
    return {
        "status": "failed",
        "run_id": "",
        "run_dir": "",
        "run_manifest_path": "",
        "validation_path": "",
        "produced_files": [],
        "error": "",
    }


def _parse_payload(payload: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    unknown_keys = sorted(set(payload.keys()) - ALLOWED_INPUT_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown payload keys: {unknown_keys}")

    params_text = payload.get("params_text")
    schedule_text = payload.get("schedule_text")
    out_root = payload.get("out_root")
    run_id = payload.get("run_id")

    if not isinstance(params_text, str):
        raise ValueError("params_text must be a string")
    if not isinstance(schedule_text, str):
        raise ValueError("schedule_text must be a string")
    if out_root is not None and not isinstance(out_root, str):
        raise ValueError("out_root must be a string when provided")
    if run_id is not None and not isinstance(run_id, str):
        raise ValueError("run_id must be a string when provided")

    normalized_out_root = out_root.strip() if isinstance(out_root, str) else ""
    normalized_run_id = run_id.strip() if isinstance(run_id, str) else ""
    return (
        params_text,
        schedule_text,
        normalized_out_root or None,
        normalized_run_id or None,
    )


def _extract_from_stdout(stdout: str) -> tuple[str, str]:
    run_id_match = None
    run_dir_match = None
    for match in RUN_ID_RE.finditer(stdout):
        run_id_match = match
    for match in RUN_DIR_RE.finditer(stdout):
        run_dir_match = match
    run_id = run_id_match.group(1).strip() if run_id_match else ""
    run_dir = run_dir_match.group(1).strip() if run_dir_match else ""
    return run_id, run_dir


def _collect_produced_files(run_dir: Path | None) -> list[str]:
    if run_dir is None or not run_dir.exists() or not run_dir.is_dir():
        return []

    out: list[str] = []
    for path in sorted(run_dir.rglob("*"), key=lambda p: str(p)):
        if path.is_file():
            out.append(str(path.resolve()))
    return out


def _write_tool_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_cholla(payload: dict[str, Any]) -> dict[str, Any]:
    """Run backend smoke script from explicit params/schedule text payload."""

    result = _default_result()

    try:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        repo_root = _repo_root()
        script_path = repo_root / "scripts" / "colab_smoke.sh"
        if not script_path.exists():
            raise FileNotFoundError(f"missing backend script: {script_path}")

        params_text, schedule_text, out_root, run_id_override = _parse_payload(payload)

        tools_runs_root = repo_root / "agent" / "tools" / "_runs"
        tools_runs_root.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix="run_cholla_", dir=str(tools_runs_root))).resolve()
        inputs_dir = stage_dir / "inputs"
        logs_dir = stage_dir / "logs"
        artifacts_dir = stage_dir / "artifacts"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        params_path = inputs_dir / "params.txt"
        schedule_path = inputs_dir / "scale_outputs.txt"
        params_path.write_text(params_text, encoding="utf-8")
        schedule_path.write_text(schedule_text, encoding="utf-8")

        backend_out_root = _resolve_out_root(repo_root, out_root, stage_dir)
        backend_out_root.mkdir(parents=True, exist_ok=True)

        cmd = [
            "bash",
            str(script_path),
            "--params",
            str(params_path),
            "--schedule",
            str(schedule_path),
            "--out-root",
            str(backend_out_root),
        ]
        if run_id_override:
            cmd.extend(["--run-id", run_id_override])

        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )

        stdout_path = logs_dir / "colab_smoke.stdout.log"
        stderr_path = logs_dir / "colab_smoke.stderr.log"
        tool_log_path = logs_dir / "run_cholla.log"
        stdout_path.write_text(proc.stdout, encoding="utf-8")
        stderr_path.write_text(proc.stderr, encoding="utf-8")
        tool_log_path.write_text(
            "\n".join(
                [
                    f"timestamp_utc={_utc_now()}",
                    f"command={' '.join(cmd)}",
                    f"cwd={repo_root}",
                    f"returncode={proc.returncode}",
                    "",
                    "STDOUT:",
                    proc.stdout,
                    "",
                    "STDERR:",
                    proc.stderr,
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        parsed_run_id, parsed_run_dir = _extract_from_stdout(proc.stdout)
        run_id = parsed_run_id or run_id_override or ""
        run_dir = parsed_run_dir or (str((backend_out_root / run_id).resolve()) if run_id else "")

        run_dir_path = Path(run_dir).resolve() if run_dir else None
        validation_candidate = run_dir_path / "validator.json" if run_dir_path else None
        validation_path = (
            str(validation_candidate.resolve())
            if validation_candidate is not None and validation_candidate.exists()
            else ""
        )
        produced_files = _collect_produced_files(run_dir_path)

        status = "success" if proc.returncode == 0 else "failed"
        error = "" if proc.returncode == 0 else f"colab_smoke exited with code {proc.returncode}"
        if proc.returncode != 0 and proc.stderr.strip():
            error = f"{error}: {proc.stderr.strip().splitlines()[-1]}"

        tool_manifest_payload = {
            "tool": "run_cholla",
            "timestamp_utc": _utc_now(),
            "status": status,
            "exit_code": proc.returncode,
            "command": cmd,
            "run_id": run_id,
            "run_dir": run_dir,
            "validation_path": validation_path,
            "produced_files": produced_files,
            "staging": {
                "stage_dir": str(stage_dir),
                "params_path": str(params_path),
                "schedule_path": str(schedule_path),
                "stdout_log_path": str(stdout_path),
                "stderr_log_path": str(stderr_path),
                "tool_log_path": str(tool_log_path),
            },
        }
        run_manifest_path = artifacts_dir / "run_manifest.tool.json"
        _write_tool_manifest(run_manifest_path, tool_manifest_payload)

        result.update(
            {
                "status": status,
                "run_id": run_id,
                "run_dir": run_dir,
                "run_manifest_path": str(run_manifest_path),
                "validation_path": validation_path,
                "produced_files": produced_files,
                "error": error,
                "exit_code": proc.returncode,
                "stdout_log_path": str(stdout_path),
                "stderr_log_path": str(stderr_path),
                "tool_log_path": str(tool_log_path),
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = str(exc)
        return result


__all__ = ["run_cholla"]
