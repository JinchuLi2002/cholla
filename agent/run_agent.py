#!/usr/bin/env python3
"""Tier-4 deterministic orchestrator for proposal/validation/execution/metric/history."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from execute_run import execute_run
from metric import compute_metric
from propose_params import propose
from validate_params import ValidationError, validate_params


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path(repo_root: Path, value: Path) -> Path:
    if value.is_absolute():
        return value.resolve()
    return (repo_root / value).resolve()


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected mapping spec at {path}")
    return payload


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _derive_agent_run_id(seed: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"seed{seed}_{stamp}"


def _metric_path_from_execution_record(execution_record: dict[str, Any]) -> Path:
    paths = execution_record.get("paths", {})
    if isinstance(paths, dict):
        iteration_dir = paths.get("iteration_dir")
        if isinstance(iteration_dir, str) and iteration_dir:
            return Path(iteration_dir) / "metric.json"
    return Path("metric.json")


def _run_id_from_records(execution_record: dict[str, Any], manifest: dict[str, Any]) -> str:
    run_id = manifest.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id

    paths = execution_record.get("paths", {})
    if isinstance(paths, dict):
        run_dir = paths.get("backend_run_dir")
        if isinstance(run_dir, str) and run_dir:
            return Path(run_dir).name
    return ""


def _status_from_records(
    validated_params: dict[str, Any],
    execution_record: dict[str, Any],
    manifest: dict[str, Any],
) -> str:
    backend = execution_record.get("backend", {})
    rc = backend.get("returncode") if isinstance(backend, dict) else None
    record_params = execution_record.get("proposed_params")
    manifest_status = manifest.get("status")
    validator_passed = manifest.get("validator_passed")

    params_match = isinstance(record_params, dict) and record_params == validated_params
    if rc == 0 and manifest_status == "success" and validator_passed is True and params_match:
        return "success"
    return "failed"


def _append_history(history_path: Path, record: dict[str, Any]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Tier-4 agent loop.")
    parser.add_argument("--seed", type=int, required=True, help="Deterministic seed.")
    parser.add_argument("--iters", type=int, required=True, help="Number of iterations (>=1).")
    parser.add_argument(
        "--agent-run-id",
        type=str,
        default="",
        help="Run grouping ID. Default: seed + UTC timestamp.",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=Path("agent/spec/param_space_v0.yaml"),
        help="Parameter spec path.",
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
    parser.add_argument(
        "--history",
        type=Path,
        default=Path("agent/history/history.jsonl"),
        help="Append-only history JSONL path.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop loop at first failed iteration.",
    )
    args = parser.parse_args()

    if args.iters < 1:
        print("RUN_AGENT_ERROR: --iters must be >= 1", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    agent_run_id = args.agent_run_id or _derive_agent_run_id(args.seed)
    spec_path = _resolve_path(repo_root, args.spec)
    template_params_path = _resolve_path(repo_root, args.template_params)
    template_schedule_path = _resolve_path(repo_root, args.template_schedule)
    history_path = _resolve_path(repo_root, args.history)

    try:
        spec = _load_yaml(spec_path)
    except Exception as exc:  # noqa: BLE001
        print(f"RUN_AGENT_ERROR: failed to load spec: {exc}", file=sys.stderr)
        return 2

    success_count = 0
    failed_count = 0

    for iteration in range(args.iters):
        timestamp = _utc_now()

        try:
            proposed = propose(seed=args.seed, iteration=iteration)
            validated = validate_params(spec=spec, params=proposed)
        except (ValidationError, ValueError) as exc:
            failed_count += 1
            history_record = {
                "timestamp_utc": timestamp,
                "agent_run_id": agent_run_id,
                "iteration": iteration,
                "seed": args.seed,
                "params": {},
                "RUN_ID": "",
                "metric": {"name": "snapshot_file_count", "value": None, "path": ""},
                "status": "failed",
                "manifest_path": "",
                "error": f"proposal_or_validation_error: {exc}",
            }
            _append_history(history_path, history_record)
            print(json.dumps(history_record, sort_keys=True))
            if args.fail_fast:
                break
            continue

        try:
            execution_record = execute_run(
                repo_root=repo_root,
                seed=args.seed,
                iteration=iteration,
                agent_run_id=agent_run_id,
                spec_path=spec_path,
                template_params_path=template_params_path,
                template_schedule_path=template_schedule_path,
            )
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            history_record = {
                "timestamp_utc": timestamp,
                "agent_run_id": agent_run_id,
                "iteration": iteration,
                "seed": args.seed,
                "params": validated,
                "RUN_ID": "",
                "metric": {"name": "snapshot_file_count", "value": None, "path": ""},
                "status": "failed",
                "manifest_path": "",
                "error": f"execute_error: {exc}",
            }
            _append_history(history_path, history_record)
            print(json.dumps(history_record, sort_keys=True))
            if args.fail_fast:
                break
            continue

        metric_payload = compute_metric(execution_record)
        metric_path = _metric_path_from_execution_record(execution_record)
        metric_path.parent.mkdir(parents=True, exist_ok=True)
        metric_path.write_text(json.dumps(metric_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        manifest_path = execution_record.get("paths", {}).get("run_manifest_path", "")
        manifest = _load_json_if_exists(Path(manifest_path)) if isinstance(manifest_path, str) else {}

        run_id = _run_id_from_records(execution_record, manifest)
        status = _status_from_records(validated, execution_record, manifest)

        history_record = {
            "timestamp_utc": timestamp,
            "agent_run_id": agent_run_id,
            "iteration": iteration,
            "seed": args.seed,
            "params": validated,
            "RUN_ID": run_id,
            "metric": {
                "name": metric_payload.get("metric_name"),
                "value": metric_payload.get("metric_value"),
                "path": str(metric_path),
            },
            "status": status,
            "manifest_path": manifest_path if isinstance(manifest_path, str) else "",
        }

        _append_history(history_path, history_record)
        print(json.dumps(history_record, sort_keys=True))

        if status == "success":
            success_count += 1
        else:
            failed_count += 1
            if args.fail_fast:
                break

    summary = {
        "agent_run_id": agent_run_id,
        "seed": args.seed,
        "iters_requested": args.iters,
        "iters_succeeded": success_count,
        "iters_failed": failed_count,
        "history_path": str(history_path),
    }
    print(json.dumps({"summary": summary}, sort_keys=True))

    return 0 if success_count == args.iters else 1


if __name__ == "__main__":
    raise SystemExit(main())
