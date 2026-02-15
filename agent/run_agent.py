#!/usr/bin/env python3
"""Tier-4 deterministic orchestrator for proposal/validation/execution/metric/history."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from execute_run import _render_params, execute_run
from experiment_bundle import derive_experiment_id, write_experiment_bundle
from metric_v2 import compute_metric
from propose_params import propose
from validate_params import ValidationError, validate_params


# History schema:
# - Iteration record: one JSON object per attempted iteration (`record_type=iteration`)
# - Run-end record: one JSON object at loop end (`record_type=run_end`) with termination reason
# - Every record includes `experiment_id` for bundle-level provenance.
# - Iteration record includes `resources` with lightweight accounting fields:
#   `walltime_sec` and `output_bytes` (both informational, never used for control flow)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path(repo_root: Path, value: Path) -> Path:
    if value.is_absolute():
        return value.resolve()
    return (repo_root / value).resolve()


def _path_for_bundle(repo_root: Path, value: Path) -> str:
    resolved = value.resolve()
    try:
        return str(resolved.relative_to(repo_root.resolve()))
    except ValueError:
        return str(resolved)


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


def _load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError(f"history line {lineno} is not a JSON object")
        records.append(payload)
    return records


def _first_param_diff(expected: dict[str, Any], observed: dict[str, Any]) -> str:
    expected_keys = set(expected)
    observed_keys = set(observed)
    if expected_keys != observed_keys:
        missing = sorted(expected_keys - observed_keys)
        extra = sorted(observed_keys - expected_keys)
        if missing:
            return f"missing keys: {missing}"
        return f"unexpected keys: {extra}"

    for key in sorted(expected_keys):
        expected_value = expected[key]
        observed_value = observed[key]
        if type(expected_value) is not type(observed_value):  # noqa: E721
            return (
                f"type mismatch for key '{key}': expected {type(expected_value).__name__}, "
                f"got {type(observed_value).__name__}"
            )
        if expected_value != observed_value:
            return f"value mismatch for key '{key}': expected {expected_value!r}, got {observed_value!r}"
    return ""


def _replay_template_paths(
    *,
    repo_root: Path,
    bundle_dir: Path,
    config: dict[str, Any],
) -> tuple[Path, Path]:
    bundled_params = bundle_dir / "template_params.txt"
    bundled_schedule = bundle_dir / "template_schedule.txt"
    if bundled_params.exists() and bundled_schedule.exists():
        return (bundled_params, bundled_schedule)

    effective = config.get("effective_args", {})
    if not isinstance(effective, dict):
        raise ValueError("bundle config missing 'effective_args' for template path resolution")

    params_rel = effective.get("template_params_path")
    schedule_rel = effective.get("template_schedule_path")
    if not isinstance(params_rel, str) or not params_rel:
        raise ValueError("bundle config missing effective_args.template_params_path")
    if not isinstance(schedule_rel, str) or not schedule_rel:
        raise ValueError("bundle config missing effective_args.template_schedule_path")

    params_path = (repo_root / params_rel).resolve()
    schedule_path = (repo_root / schedule_rel).resolve()
    if not params_path.exists():
        raise FileNotFoundError(f"missing template params for replay: {params_path}")
    if not schedule_path.exists():
        raise FileNotFoundError(f"missing template schedule for replay: {schedule_path}")
    return (params_path, schedule_path)


def _run_id_from_params(*, git_sha: str, params_text: str, schedule_bytes: bytes) -> str:
    git_sha_short = "unknown"
    if git_sha and git_sha != "unknown":
        git_sha_short = git_sha[:12]
    input_hash = hashlib.sha256(params_text.encode("utf-8") + schedule_bytes).hexdigest()
    return f"{git_sha_short}_smoke_cosmo_{input_hash[:16]}"


def _run_replay_mode(*, repo_root: Path, experiment_id: str) -> int:
    bundle_dir = repo_root / "agent" / "experiments" / experiment_id
    if not bundle_dir.exists():
        print(f"REPLAY_FAIL: missing experiment bundle: {bundle_dir}", file=sys.stderr)
        return 1

    seed_path = bundle_dir / "seed.txt"
    if not seed_path.exists():
        print(f"REPLAY_FAIL: missing seed file: {seed_path}", file=sys.stderr)
        return 1
    try:
        seed = int(seed_path.read_text(encoding="utf-8").strip())
    except ValueError as exc:
        print(f"REPLAY_FAIL: invalid seed in {seed_path}: {exc}", file=sys.stderr)
        return 1

    spec_path = bundle_dir / "param_space.yaml"
    if not spec_path.exists():
        print(f"REPLAY_FAIL: missing param_space.yaml in {bundle_dir}", file=sys.stderr)
        return 1
    try:
        spec = _load_yaml(spec_path)
    except Exception as exc:  # noqa: BLE001
        print(f"REPLAY_FAIL: failed to load spec from {spec_path}: {exc}", file=sys.stderr)
        return 1

    config = _load_json_if_exists(bundle_dir / "config.json")
    summary = _load_json_if_exists(bundle_dir / "summary.json")

    try:
        template_params_path, template_schedule_path = _replay_template_paths(
            repo_root=repo_root,
            bundle_dir=bundle_dir,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"REPLAY_FAIL: failed template resolution: {exc}", file=sys.stderr)
        return 1

    history_path = bundle_dir / "history_experiment.jsonl"
    if not history_path.exists():
        history_path = bundle_dir / "history.jsonl"
    if not history_path.exists():
        print(f"REPLAY_FAIL: missing history file in {bundle_dir}", file=sys.stderr)
        return 1

    try:
        history_records = _load_jsonl_records(history_path)
    except Exception as exc:  # noqa: BLE001
        print(f"REPLAY_FAIL: failed to parse history file {history_path}: {exc}", file=sys.stderr)
        return 1

    iteration_records_all = [r for r in history_records if r.get("record_type") == "iteration"]
    iteration_records = [r for r in iteration_records_all if r.get("experiment_id") == experiment_id]
    if not iteration_records:
        iteration_records = iteration_records_all
    if not iteration_records:
        print(f"REPLAY_FAIL: no iteration records found in {history_path}", file=sys.stderr)
        return 1

    template_text = template_params_path.read_text(encoding="utf-8")
    schedule_bytes = template_schedule_path.read_bytes()

    git_sha = summary.get("git_sha")
    if not isinstance(git_sha, str) or not git_sha:
        git_sha = "unknown"

    params_checked = 0
    run_id_checked = 0

    for record in iteration_records:
        iteration = record.get("iteration")
        if not isinstance(iteration, int):
            print(f"REPLAY_FAIL: invalid iteration value: {iteration!r}", file=sys.stderr)
            return 1

        try:
            proposed = propose(seed=seed, iteration=iteration)
            expected_params = validate_params(spec=spec, params=proposed)
        except Exception as exc:  # noqa: BLE001
            print(
                f"REPLAY_FAIL: failed to recompute params for iteration {iteration}: {exc}",
                file=sys.stderr,
            )
            return 1

        status = record.get("status")
        observed_params = record.get("params")
        should_check_params = status == "success" or (
            isinstance(observed_params, dict) and len(observed_params) > 0
        )
        if should_check_params:
            if not isinstance(observed_params, dict):
                print(
                    f"REPLAY_FAIL: iteration {iteration} has invalid params payload: {type(observed_params).__name__}",
                    file=sys.stderr,
                )
                return 1
            diff = _first_param_diff(expected_params, observed_params)
            if diff:
                print(
                    f"REPLAY_FAIL: iteration {iteration} params mismatch: {diff}",
                    file=sys.stderr,
                )
                return 1
            params_checked += 1

        observed_run_id = record.get("RUN_ID")
        should_check_run_id = status == "success" or (
            isinstance(observed_run_id, str) and observed_run_id != ""
        )
        if should_check_run_id:
            if not isinstance(observed_run_id, str):
                print(
                    f"REPLAY_FAIL: iteration {iteration} has invalid RUN_ID type: {type(observed_run_id).__name__}",
                    file=sys.stderr,
                )
                return 1
            expected_run_id = _run_id_from_params(
                git_sha=git_sha,
                params_text=_render_params(template_text, expected_params),
                schedule_bytes=schedule_bytes,
            )
            if observed_run_id != expected_run_id:
                print(
                    "REPLAY_FAIL: iteration "
                    f"{iteration} RUN_ID mismatch: expected {expected_run_id}, got {observed_run_id}",
                    file=sys.stderr,
                )
                return 1
            run_id_checked += 1

    print(
        "REPLAY_OK "
        f"experiment_id={experiment_id} "
        f"iterations_total={len(iteration_records)} "
        f"params_checked={params_checked} "
        f"run_id_checked={run_id_checked} "
        f"history={history_path.name}"
    )
    return 0


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


def _resources_from_execution_record(execution_record: dict[str, Any]) -> dict[str, Any]:
    resources = execution_record.get("resources", {})
    if not isinstance(resources, dict):
        resources = {}

    walltime_sec = resources.get("walltime_sec")
    output_bytes = resources.get("output_bytes")

    walltime_value = (
        float(walltime_sec)
        if isinstance(walltime_sec, (int, float)) and not isinstance(walltime_sec, bool)
        else None
    )
    output_value = int(output_bytes) if isinstance(output_bytes, int) and not isinstance(output_bytes, bool) else None
    return {
        "walltime_sec": walltime_value,
        "output_bytes": output_value,
    }


def _window_variance(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def _guardrail_termination_reason(
    *,
    failures: int,
    max_failures: int,
    total_runs: int,
    max_total_runs: int,
    stop_on_converged: bool,
    successful_metric_scalars: list[float],
    convergence_window: int,
    convergence_tol: float,
) -> str:
    # Order is intentional and deterministic, per Phase-2 requirements.
    if failures > max_failures:
        return "max_failures_exceeded"
    if total_runs >= max_total_runs:
        return "max_total_runs_reached"
    if stop_on_converged and len(successful_metric_scalars) >= convergence_window:
        last_k = successful_metric_scalars[-convergence_window:]
        if _window_variance(last_k) < convergence_tol:
            return "converged"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Tier-4 agent loop.")
    parser.add_argument("--seed", type=int, default=None, help="Deterministic seed.")
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--iters", type=int, default=None, help="Number of iterations (>=1).")
    mode_group.add_argument(
        "--replay",
        type=str,
        default="",
        help="Replay integrity mode for an experiment id under agent/experiments/<id>/.",
    )
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
    parser.add_argument(
        "--stop-on-converged",
        action="store_true",
        help="Stop early when metric variance over recent successful iterations is below tolerance.",
    )
    parser.add_argument(
        "--convergence-window",
        type=int,
        default=5,
        help="Number of recent successful metric scalars used for convergence checks.",
    )
    parser.add_argument(
        "--convergence-tol",
        type=float,
        default=1e-6,
        help="Convergence tolerance: stop when variance(last K successful scalars) < EPS.",
    )
    parser.add_argument(
        "--max-total-runs",
        type=int,
        default=None,
        help="Maximum total attempted iterations. Default: --iters.",
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=3,
        help="Maximum allowed failures before stopping. Stops when failures > max_failures.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]

    if args.replay:
        if args.seed is not None:
            print("RUN_AGENT_ERROR: --seed cannot be used with --replay", file=sys.stderr)
            return 2
        return _run_replay_mode(repo_root=repo_root, experiment_id=args.replay)

    if args.seed is None:
        print("RUN_AGENT_ERROR: --seed is required in normal mode", file=sys.stderr)
        return 2
    if args.iters is None:
        print("RUN_AGENT_ERROR: --iters is required in normal mode", file=sys.stderr)
        return 2
    if args.iters < 1:
        print("RUN_AGENT_ERROR: --iters must be >= 1", file=sys.stderr)
        return 2
    if args.convergence_window < 1:
        print("RUN_AGENT_ERROR: --convergence-window must be >= 1", file=sys.stderr)
        return 2
    if args.convergence_tol < 0:
        print("RUN_AGENT_ERROR: --convergence-tol must be >= 0", file=sys.stderr)
        return 2
    if args.max_failures < 0:
        print("RUN_AGENT_ERROR: --max-failures must be >= 0", file=sys.stderr)
        return 2

    agent_run_id = args.agent_run_id or _derive_agent_run_id(args.seed)
    spec_path = _resolve_path(repo_root, args.spec)
    template_params_path = _resolve_path(repo_root, args.template_params)
    template_schedule_path = _resolve_path(repo_root, args.template_schedule)
    history_path = _resolve_path(repo_root, args.history)
    max_total_runs = args.max_total_runs if args.max_total_runs is not None else args.iters
    cli_args_payload = {
        "seed": args.seed,
        "iters": args.iters,
        "agent_run_id": args.agent_run_id,
        "spec": str(args.spec),
        "template_params": str(args.template_params),
        "template_schedule": str(args.template_schedule),
        "history": str(args.history),
        "fail_fast": args.fail_fast,
        "stop_on_converged": args.stop_on_converged,
        "convergence_window": args.convergence_window,
        "convergence_tol": args.convergence_tol,
        "max_total_runs": args.max_total_runs,
        "max_failures": args.max_failures,
    }

    if max_total_runs < 1:
        print("RUN_AGENT_ERROR: --max-total-runs must be >= 1", file=sys.stderr)
        return 2

    try:
        spec = _load_yaml(spec_path)
    except Exception as exc:  # noqa: BLE001
        print(f"RUN_AGENT_ERROR: failed to load spec: {exc}", file=sys.stderr)
        return 2

    hash_args_payload = {
        "cli_args": cli_args_payload,
        "effective_defaults": {
            "max_total_runs": max_total_runs,
            "convergence_window": args.convergence_window,
            "convergence_tol": args.convergence_tol,
            "max_failures": args.max_failures,
            "stop_on_converged": args.stop_on_converged,
        },
    }
    experiment_id = derive_experiment_id(
        seed=args.seed,
        spec_path=spec_path,
        args_for_hash=hash_args_payload,
    )

    run_history_records: list[dict[str, Any]] = []

    def _record_history(record: dict[str, Any]) -> None:
        record["experiment_id"] = experiment_id
        run_history_records.append(record)
        _append_history(history_path, record)
        print(json.dumps(record, sort_keys=True))

    success_count = 0
    failed_count = 0
    total_runs_attempted = 0
    successful_metric_scalars: list[float] = []
    terminate_reason = "iters_completed"

    for iteration in range(args.iters):
        timestamp = _utc_now()
        total_runs_attempted += 1
        iteration_failed = False

        try:
            proposed = propose(seed=args.seed, iteration=iteration)
            validated = validate_params(spec=spec, params=proposed)
        except (ValidationError, ValueError) as exc:
            failed_count += 1
            iteration_failed = True
            history_record = {
                "record_type": "iteration",
                "timestamp_utc": timestamp,
                "agent_run_id": agent_run_id,
                "iteration": iteration,
                "seed": args.seed,
                "params": {},
                "RUN_ID": "",
                "metric": {"name": "density_mean_var_v2", "scalar": None, "value": None, "path": ""},
                "resources": {"walltime_sec": None, "output_bytes": None},
                "status": "failed",
                "manifest_path": "",
                "error": f"proposal_or_validation_error: {exc}",
            }
            _record_history(history_record)

            terminate_reason = _guardrail_termination_reason(
                failures=failed_count,
                max_failures=args.max_failures,
                total_runs=total_runs_attempted,
                max_total_runs=max_total_runs,
                stop_on_converged=args.stop_on_converged,
                successful_metric_scalars=successful_metric_scalars,
                convergence_window=args.convergence_window,
                convergence_tol=args.convergence_tol,
            )
            if terminate_reason:
                break
            if args.fail_fast and iteration_failed:
                terminate_reason = "fail_fast"
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
            iteration_failed = True
            history_record = {
                "record_type": "iteration",
                "timestamp_utc": timestamp,
                "agent_run_id": agent_run_id,
                "iteration": iteration,
                "seed": args.seed,
                "params": validated,
                "RUN_ID": "",
                "metric": {"name": "density_mean_var_v2", "scalar": None, "value": None, "path": ""},
                "resources": {"walltime_sec": None, "output_bytes": None},
                "status": "failed",
                "manifest_path": "",
                "error": f"execute_error: {exc}",
            }
            _record_history(history_record)

            terminate_reason = _guardrail_termination_reason(
                failures=failed_count,
                max_failures=args.max_failures,
                total_runs=total_runs_attempted,
                max_total_runs=max_total_runs,
                stop_on_converged=args.stop_on_converged,
                successful_metric_scalars=successful_metric_scalars,
                convergence_window=args.convergence_window,
                convergence_tol=args.convergence_tol,
            )
            if terminate_reason:
                break
            if args.fail_fast and iteration_failed:
                terminate_reason = "fail_fast"
                break
            continue

        paths = execution_record.get("paths", {})
        if not isinstance(paths, dict):
            paths = {}

        backend_run_dir = paths.get("backend_run_dir", "")
        if not isinstance(backend_run_dir, str):
            backend_run_dir = ""

        manifest_path = paths.get("run_manifest_path", "")
        if not isinstance(manifest_path, str):
            manifest_path = ""

        try:
            metric_payload = compute_metric(
                run_dir=backend_run_dir,
                manifest_path=manifest_path or None,
            )
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            iteration_failed = True
            history_record = {
                "record_type": "iteration",
                "timestamp_utc": timestamp,
                "agent_run_id": agent_run_id,
                "iteration": iteration,
                "seed": args.seed,
                "params": validated,
                "RUN_ID": "",
                "metric": {"name": "density_mean_var_v2", "scalar": None, "value": None, "path": ""},
                "resources": _resources_from_execution_record(execution_record),
                "status": "failed",
                "manifest_path": manifest_path,
                "error": f"metric_error: {exc}",
            }
            _record_history(history_record)

            terminate_reason = _guardrail_termination_reason(
                failures=failed_count,
                max_failures=args.max_failures,
                total_runs=total_runs_attempted,
                max_total_runs=max_total_runs,
                stop_on_converged=args.stop_on_converged,
                successful_metric_scalars=successful_metric_scalars,
                convergence_window=args.convergence_window,
                convergence_tol=args.convergence_tol,
            )
            if terminate_reason:
                break
            if args.fail_fast and iteration_failed:
                terminate_reason = "fail_fast"
                break
            continue

        metric_path = _metric_path_from_execution_record(execution_record)
        metric_path.parent.mkdir(parents=True, exist_ok=True)
        metric_path.write_text(json.dumps(metric_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        manifest = _load_json_if_exists(Path(manifest_path)) if manifest_path else {}

        run_id = _run_id_from_records(execution_record, manifest)
        status = _status_from_records(validated, execution_record, manifest)
        scalar_raw = metric_payload.get("scalar", metric_payload.get("metric_value"))
        metric_scalar = (
            float(scalar_raw)
            if isinstance(scalar_raw, (int, float)) and not isinstance(scalar_raw, bool)
            else None
        )

        history_record = {
            "record_type": "iteration",
            "timestamp_utc": timestamp,
            "agent_run_id": agent_run_id,
            "iteration": iteration,
            "seed": args.seed,
            "params": validated,
            "RUN_ID": run_id,
            "metric": {
                "name": metric_payload.get("metric_name"),
                "scalar": metric_scalar,
                "value": metric_scalar,
                "path": str(metric_path),
                "source": metric_payload.get("source"),
                "snapshot_path": metric_payload.get("snapshot_path", ""),
            },
            "resources": _resources_from_execution_record(execution_record),
            "status": status,
            "manifest_path": manifest_path,
        }

        _record_history(history_record)

        if status == "success":
            success_count += 1
            if metric_scalar is not None:
                successful_metric_scalars.append(metric_scalar)
        else:
            failed_count += 1
            iteration_failed = True

        terminate_reason = _guardrail_termination_reason(
            failures=failed_count,
            max_failures=args.max_failures,
            total_runs=total_runs_attempted,
            max_total_runs=max_total_runs,
            stop_on_converged=args.stop_on_converged,
            successful_metric_scalars=successful_metric_scalars,
            convergence_window=args.convergence_window,
            convergence_tol=args.convergence_tol,
        )
        if terminate_reason:
            break
        if args.fail_fast and iteration_failed:
            terminate_reason = "fail_fast"
            break

    run_end_record = {
        "record_type": "run_end",
        "timestamp_utc": _utc_now(),
        "agent_run_id": agent_run_id,
        "seed": args.seed,
        "iters_requested": args.iters,
        "total_runs_attempted": total_runs_attempted,
        "iters_succeeded": success_count,
        "iters_failed": failed_count,
        "terminate_reason": terminate_reason,
        "guardrails": {
            "stop_on_converged": args.stop_on_converged,
            "convergence_window": args.convergence_window,
            "convergence_tol": args.convergence_tol,
            "max_total_runs": max_total_runs,
            "max_failures": args.max_failures,
            "fail_fast": args.fail_fast,
        },
        "successful_metric_scalars_count": len(successful_metric_scalars),
    }
    _record_history(run_end_record)

    effective_args_payload = {
        "seed": args.seed,
        "iters": args.iters,
        "agent_run_id": agent_run_id,
        "spec_path": _path_for_bundle(repo_root, spec_path),
        "template_params_path": _path_for_bundle(repo_root, template_params_path),
        "template_schedule_path": _path_for_bundle(repo_root, template_schedule_path),
        "history_path": _path_for_bundle(repo_root, history_path),
        "max_total_runs": max_total_runs,
        "max_failures": args.max_failures,
        "stop_on_converged": args.stop_on_converged,
        "convergence_window": args.convergence_window,
        "convergence_tol": args.convergence_tol,
        "fail_fast": args.fail_fast,
    }
    bundle_config = {
        "experiment_id": experiment_id,
        "cli_args": cli_args_payload,
        "effective_args": effective_args_payload,
        "metric": {
            "name": "density_mean_var_v2",
            "objective": "maximize_scalar",
            "best_selection_rule": "max_scalar_tie_breaker=earliest_iteration",
        },
        "guardrails": {
            "stop_on_converged": args.stop_on_converged,
            "convergence_window": args.convergence_window,
            "convergence_tol": args.convergence_tol,
            "max_total_runs": max_total_runs,
            "max_failures": args.max_failures,
            "fail_fast": args.fail_fast,
        },
    }

    try:
        bundle_dir = write_experiment_bundle(
            repo_root=repo_root,
            experiment_id=experiment_id,
            config=bundle_config,
            spec_path=spec_path,
            template_params_path=template_params_path,
            template_schedule_path=template_schedule_path,
            seed=args.seed,
            history_records=run_history_records,
            history_source_path=history_path,
            total_iterations_attempted=total_runs_attempted,
            iters_succeeded=success_count,
            iters_failed=failed_count,
            termination_reason=terminate_reason,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"RUN_AGENT_ERROR: failed to write experiment bundle: {exc}", file=sys.stderr)
        return 2

    summary = {
        "experiment_id": experiment_id,
        "experiment_bundle_path": _path_for_bundle(repo_root, bundle_dir),
        "agent_run_id": agent_run_id,
        "seed": args.seed,
        "iters_requested": args.iters,
        "total_iterations_attempted": total_runs_attempted,
        "total_runs_attempted": total_runs_attempted,
        "iters_succeeded": success_count,
        "iters_failed": failed_count,
        "terminate_reason": terminate_reason,
        "guardrails": {
            "stop_on_converged": args.stop_on_converged,
            "convergence_window": args.convergence_window,
            "convergence_tol": args.convergence_tol,
            "max_total_runs": max_total_runs,
            "max_failures": args.max_failures,
            "fail_fast": args.fail_fast,
        },
        "successful_metric_scalars_count": len(successful_metric_scalars),
        "history_path": str(history_path),
    }
    print(json.dumps({"summary": summary}, sort_keys=True))

    if terminate_reason in {"max_failures_exceeded", "fail_fast"}:
        return 1
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
