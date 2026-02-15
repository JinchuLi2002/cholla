#!/usr/bin/env python3
"""Tier 4 backend adapter: render run-local inputs and execute one iteration."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from propose_params import propose
from validate_params import validate_params


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _render_params(template_text: str, overrides: dict[str, Any]) -> str:
    """Render params text by replacing key=value lines for matching keys."""
    rendered_lines: list[str] = []
    remaining = {k: _format_value(v) for k, v in overrides.items()}

    for line in template_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rendered_lines.append(line)
            continue

        key, _old_value = line.split("=", 1)
        key = key.strip()
        if key in remaining:
            rendered_lines.append(f"{key}={remaining.pop(key)}")
        else:
            rendered_lines.append(line)

    if remaining:
        rendered_lines.append("")
        rendered_lines.append("# Added by agent/execute_run.py")
        for key in sorted(remaining):
            rendered_lines.append(f"{key}={remaining[key]}")

    return "\n".join(rendered_lines) + "\n"


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _derive_agent_run_id(seed: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"seed{seed}_{stamp}"


def execute_run(
    repo_root: Path,
    seed: int,
    iteration: int,
    agent_run_id: str,
    spec_path: Path,
    template_params_path: Path,
    template_schedule_path: Path,
) -> dict[str, Any]:
    if iteration < 0:
        raise ValueError("iteration must be >= 0")
    if not template_params_path.exists():
        raise FileNotFoundError(f"missing template params: {template_params_path}")
    if not template_schedule_path.exists():
        raise FileNotFoundError(f"missing template schedule: {template_schedule_path}")
    if not spec_path.exists():
        raise FileNotFoundError(f"missing spec: {spec_path}")

    iter_dir = repo_root / "agent" / "runs" / agent_run_id / f"iter_{iteration}"
    inputs_dir = iter_dir / "inputs"
    logs_dir = iter_dir / "logs"
    artifacts_dir = iter_dir / "artifacts"
    iter_out_root = iter_dir / "backend_runs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    iter_out_root.mkdir(parents=True, exist_ok=True)

    template_hash_before = _sha256_path(template_params_path)

    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError(f"spec root must be a mapping: {spec_path}")

    proposed = propose(seed=seed, iteration=iteration)
    normalized = validate_params(spec=spec, params=proposed)

    run_local_params = inputs_dir / "params.txt"
    run_local_schedule = inputs_dir / "scale_outputs.txt"
    run_local_params.write_text(
        _render_params(template_params_path.read_text(encoding="utf-8"), normalized),
        encoding="utf-8",
    )
    shutil.copy2(template_schedule_path, run_local_schedule)

    cmd = [
        "bash",
        "scripts/colab_smoke.sh",
        "--params",
        str(run_local_params),
        "--schedule",
        str(run_local_schedule),
        "--out-root",
        str(iter_out_root),
    ]

    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    stdout_log = logs_dir / "execute_run.stdout.log"
    stderr_log = logs_dir / "execute_run.stderr.log"
    stdout_log.write_text(proc.stdout, encoding="utf-8")
    stderr_log.write_text(proc.stderr, encoding="utf-8")

    manifest_src = repo_root / "artifacts" / "run_manifest.json"
    validation_src = repo_root / "artifacts" / "validation.json"
    last_run_dir_src = repo_root / "artifacts" / "last_run_dir.txt"
    manifest_dst = artifacts_dir / "run_manifest.json"
    validation_dst = artifacts_dir / "validation.json"
    last_run_dir_dst = artifacts_dir / "last_run_dir.txt"

    has_manifest = _copy_if_exists(manifest_src, manifest_dst)
    has_validation = _copy_if_exists(validation_src, validation_dst)
    has_last_run = _copy_if_exists(last_run_dir_src, last_run_dir_dst)

    run_dir = ""
    if has_last_run:
        run_dir = last_run_dir_dst.read_text(encoding="utf-8").strip()
    if not run_dir and has_manifest:
        run_dir = str(_load_json(manifest_dst).get("run_dir", ""))

    template_hash_after = _sha256_path(template_params_path)
    template_unchanged = template_hash_before == template_hash_after
    if not template_unchanged:
        raise RuntimeError(
            "Template mutation detected: tests/smoke_cosmo/params.txt changed during execute_run."
        )

    manifest_payload = _load_json(manifest_dst) if has_manifest else {}
    validation_payload = _load_json(validation_dst) if has_validation else {}

    record: dict[str, Any] = {
        "agent_run_id": agent_run_id,
        "seed": seed,
        "iteration": iteration,
        "proposed_params": normalized,
        "paths": {
            "iteration_dir": str(iter_dir),
            "template_params_path": str(template_params_path),
            "template_schedule_path": str(template_schedule_path),
            "run_local_params_path": str(run_local_params),
            "run_local_schedule_path": str(run_local_schedule),
            "iter_out_root": str(iter_out_root),
            "backend_run_dir": run_dir,
            "run_manifest_path": str(manifest_dst) if has_manifest else "",
            "validation_path": str(validation_dst) if has_validation else "",
            "last_run_dir_path": str(last_run_dir_dst) if has_last_run else "",
            "stdout_log_path": str(stdout_log),
            "stderr_log_path": str(stderr_log),
        },
        "backend": {
            "command": cmd,
            "returncode": proc.returncode,
        },
        "status": {
            "template_unchanged": template_unchanged,
            "manifest_status": manifest_payload.get("status"),
            "validation_status": validation_payload.get("status"),
        },
    }

    record_path = iter_dir / "execution_record.json"
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["paths"]["execution_record_path"] = str(record_path)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one Tier 4 smoke iteration.")
    parser.add_argument("--seed", type=int, required=True, help="Seed for deterministic proposer.")
    parser.add_argument("--iter", type=int, required=True, help="Iteration index (>= 0).")
    parser.add_argument(
        "--agent-run-id",
        type=str,
        default="",
        help="Run-group id. Default is deterministic seed + UTC timestamp.",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=Path("agent/spec/param_space_v0.yaml"),
        help="Param-space spec path.",
    )
    parser.add_argument(
        "--template-params",
        type=Path,
        default=Path("tests/smoke_cosmo/params.txt"),
        help="Template params path.",
    )
    parser.add_argument(
        "--template-schedule",
        type=Path,
        default=Path("tests/smoke_cosmo/scale_outputs.txt"),
        help="Template schedule path.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    agent_run_id = args.agent_run_id or _derive_agent_run_id(args.seed)

    try:
        record = execute_run(
            repo_root=repo_root,
            seed=args.seed,
            iteration=args.iter,
            agent_run_id=agent_run_id,
            spec_path=(repo_root / args.spec).resolve(),
            template_params_path=(repo_root / args.template_params).resolve(),
            template_schedule_path=(repo_root / args.template_schedule).resolve(),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"EXECUTE_RUN_ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(record, sort_keys=True))
    return int(record["backend"]["returncode"])


if __name__ == "__main__":
    raise SystemExit(main())
