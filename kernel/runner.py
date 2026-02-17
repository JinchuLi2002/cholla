#!/usr/bin/env python3
"""Domain-agnostic kernel runner for cholla and stub execution modes."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping

import yaml

from agent.controller.history import HistoryRecordV0, HistoryWriter, utc_now
from kernel.plan_executor import execute_plan
from kernel.planspec import (
    PLAN_SPEC_SCHEMA_VERSION,
    STEP_SPEC_SCHEMA_VERSION,
    PlanSpec,
    StepSpec,
    compute_plan_id,
)
from kernel.tool_registry import tool_registry_for_domain


RUNNER_VERSION = "kernel_runner_v1"
ABS_PATH_RE = re.compile(r"^(?:[a-zA-Z]:[\\/]|/)")
COMPARE_IGNORED_KEYS = {
    "artifact_path",
    "elapsed_sec",
    "remaining_walltime_sec",
    "timestamp",
    "timestamp_utc",
    "walltime_sec",
}


@dataclass(frozen=True)
class StubConfig:
    toolpack_name: str
    tool_backend: str
    seed: int
    max_tool_calls: int | None
    experiment_id: str | None
    controller_run_id: str | None
    run_root: str | None
    history_path: str | None
    param_space_payload: dict[str, Any]
    metadata: dict[str, Any]
    raw_config: dict[str, Any]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve_path(raw_path: str | Path, *, repo_root: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def _as_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _as_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _as_optional_string(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string when provided")
    normalized = value.strip()
    return normalized if normalized else None


def _as_int(value: Any, *, label: str, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return int(value)


def _as_optional_int(value: Any, *, label: str, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return _as_int(value, label=label, minimum=minimum)


def _json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _json_load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"expected JSON object at {path}")
    return dict(payload)


def _yaml_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(dict(payload), sort_keys=True), encoding="utf-8")


def _json_print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), sort_keys=True))


def _git_head_sha(repo_root: Path) -> str:
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


def _cli_invocation(*, argv: list[str], module_name: str) -> str:
    args = " ".join(shlex.quote(item) for item in argv[1:])
    return f"python -m {module_name} {args}".rstrip()


def _load_stub_config(config_path: Path) -> StubConfig:
    raw_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, Mapping):
        raise ValueError(f"expected mapping YAML at {config_path}")
    payload = dict(raw_payload)

    domain_raw = payload.get("domain")
    if domain_raw is not None:
        domain = str(domain_raw).strip().lower()
        if domain and domain != "stub":
            raise ValueError(f"stub config domain must be 'stub' when provided, got {domain_raw!r}")

    toolpack_payload = (
        _as_mapping(payload.get("toolpack"), label="toolpack") if "toolpack" in payload else {}
    )
    toolpack_name = _as_string(toolpack_payload.get("name", "stub"), label="toolpack.name").lower()
    if toolpack_name != "stub":
        raise ValueError(f"unsupported stub toolpack: {toolpack_name!r} (expected 'stub')")

    backend_raw = toolpack_payload.get("backend", payload.get("tool_backend", "stub"))
    tool_backend = _as_string(backend_raw, label="toolpack.backend").lower()

    budgets_payload = (
        _as_mapping(payload.get("budgets"), label="budgets") if "budgets" in payload else {}
    )
    max_tool_calls = _as_optional_int(
        budgets_payload.get("max_tool_calls"),
        label="budgets.max_tool_calls",
        minimum=1,
    )
    seed = _as_int(payload.get("seed", 0), label="seed", minimum=0)

    param_space_payload = (
        _as_mapping(payload.get("param_space"), label="param_space")
        if "param_space" in payload
        else {
            "domain": "stub",
            "seed": seed,
            "toolpack": {"name": toolpack_name, "backend": tool_backend},
        }
    )
    metadata_payload = (
        _as_mapping(payload.get("metadata"), label="metadata") if "metadata" in payload else {}
    )

    return StubConfig(
        toolpack_name=toolpack_name,
        tool_backend=tool_backend,
        seed=seed,
        max_tool_calls=max_tool_calls,
        experiment_id=_as_optional_string(payload.get("experiment_id"), label="experiment_id"),
        controller_run_id=_as_optional_string(payload.get("controller_run_id"), label="controller_run_id"),
        run_root=_as_optional_string(payload.get("run_root"), label="run_root"),
        history_path=_as_optional_string(payload.get("history_path"), label="history_path"),
        param_space_payload=param_space_payload,
        metadata=metadata_payload,
        raw_config=payload,
    )


def _load_stub_plan(plan_path: Path) -> PlanSpec:
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"plan root must be a JSON object: {plan_path}")

    steps_raw = raw.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ValueError("plan.steps must be a list")

    steps: list[StepSpec] = []
    for idx, step_raw in enumerate(steps_raw):
        if not isinstance(step_raw, Mapping):
            raise ValueError(f"plan.steps[{idx}] must be an object")
        tool = _as_string(step_raw.get("tool"), label=f"plan.steps[{idx}].tool")
        payload_raw = step_raw.get("payload", {})
        if not isinstance(payload_raw, Mapping):
            raise ValueError(f"plan.steps[{idx}].payload must be an object")

        expected_artifacts_raw = step_raw.get("expected_artifacts", [])
        if not isinstance(expected_artifacts_raw, list):
            raise ValueError(f"plan.steps[{idx}].expected_artifacts must be a list")
        note_raw = step_raw.get("note")
        if note_raw is not None and not isinstance(note_raw, str):
            raise ValueError(f"plan.steps[{idx}].note must be a string or null")
        metadata_raw = step_raw.get("metadata", {})
        if not isinstance(metadata_raw, Mapping):
            raise ValueError(f"plan.steps[{idx}].metadata must be an object")

        steps.append(
            StepSpec(
                tool=tool,
                payload=dict(payload_raw),
                note=note_raw,
                expected_artifacts=[str(item) for item in expected_artifacts_raw],
                expects_run_id=bool(step_raw.get("expects_run_id", False)),
                expects_metric_scalar=bool(step_raw.get("expects_metric_scalar", False)),
                metadata=dict(metadata_raw),
                schema_version=str(step_raw.get("schema_version", STEP_SPEC_SCHEMA_VERSION)),
            )
        )

    metadata_raw = raw.get("metadata", {})
    if not isinstance(metadata_raw, Mapping):
        raise ValueError("plan.metadata must be an object")
    termination_reason_raw = raw.get("termination_reason")
    if termination_reason_raw is not None and not isinstance(termination_reason_raw, str):
        raise ValueError("plan.termination_reason must be a string or null")
    rationale_raw = raw.get("rationale", "")
    if not isinstance(rationale_raw, str):
        raise ValueError("plan.rationale must be a string")

    return PlanSpec(
        steps=steps,
        should_stop=bool(raw.get("should_stop", False)),
        termination_reason=termination_reason_raw,
        rationale=rationale_raw,
        metadata=dict(metadata_raw),
        schema_version=str(raw.get("schema_version", PLAN_SPEC_SCHEMA_VERSION)),
    )


def _infer_run_id(tool_results: list[dict[str, Any]]) -> str | None:
    for call in tool_results:
        result_raw = call.get("result")
        if not isinstance(result_raw, Mapping):
            continue
        for key in ("run_id", "RUN_ID"):
            value = result_raw.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _first_tool_error(tool_results: list[dict[str, Any]]) -> str | None:
    for call in tool_results:
        if call.get("status") == "success":
            continue
        tool = call.get("tool")
        result_raw = call.get("result")
        if isinstance(result_raw, Mapping):
            error_value = result_raw.get("error")
            if isinstance(error_value, str) and error_value:
                return f"{tool}: {error_value}"
            return f"{tool}: result={json.dumps(dict(result_raw), sort_keys=True)}"
        return f"{tool}: failed"
    return None


def _extract_metric_scalar(tool_results: list[dict[str, Any]]) -> float | None:
    for call in tool_results:
        result_raw = call.get("result")
        if not isinstance(result_raw, Mapping):
            continue
        scalar = result_raw.get("scalar")
        if isinstance(scalar, (int, float)) and not isinstance(scalar, bool):
            return float(scalar)
    return None


def _budget_snapshot(
    *,
    max_tool_calls: int,
    tool_calls_used: int,
    iterations_completed: int,
    elapsed_sec: float,
) -> dict[str, Any]:
    return {
        "max_iterations": 1,
        "max_tool_calls": max_tool_calls,
        "walltime_budget_sec": None,
        "iterations_completed": iterations_completed,
        "tool_calls_used": tool_calls_used,
        "elapsed_sec": elapsed_sec,
        "remaining_iterations": max(1 - iterations_completed, 0),
        "remaining_tool_calls": max(max_tool_calls - tool_calls_used, 0),
        "remaining_walltime_sec": None,
    }


def _normalize_for_compare(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key in sorted(value.keys()):
            key_text = str(key)
            if key_text in COMPARE_IGNORED_KEYS:
                continue
            out[key_text] = _normalize_for_compare(value[key])
        return out
    if isinstance(value, list):
        return [_normalize_for_compare(item) for item in value]
    if isinstance(value, str) and ABS_PATH_RE.match(value):
        return "<ABS_PATH>"
    return value


def _first_diff(a_value: Any, b_value: Any, *, path: str) -> str | None:
    if type(a_value) is not type(b_value):  # noqa: E721
        return f"{path}: type mismatch a={type(a_value).__name__} b={type(b_value).__name__}"

    if isinstance(a_value, dict):
        a_keys = set(a_value.keys())
        b_keys = set(b_value.keys())
        for missing in sorted(a_keys - b_keys):
            return f"{path}/{missing}: missing in replay"
        for extra in sorted(b_keys - a_keys):
            return f"{path}/{extra}: missing in stored"
        for key in sorted(a_keys):
            child = _first_diff(a_value[key], b_value[key], path=f"{path}/{key}")
            if child:
                return child
        return None

    if isinstance(a_value, list):
        if len(a_value) != len(b_value):
            return f"{path}: list length mismatch a={len(a_value)} b={len(b_value)}"
        for index, (a_item, b_item) in enumerate(zip(a_value, b_value)):
            child = _first_diff(a_item, b_item, path=f"{path}/{index}")
            if child:
                return child
        return None

    if a_value != b_value:
        return f"{path}: value mismatch a={a_value!r} b={b_value!r}"
    return None


def _resolve_stub_controller_bundle(*, bundle_path_raw: str, repo_root: Path) -> Path:
    bundle_path = _resolve_path(bundle_path_raw, repo_root=repo_root)
    if (bundle_path / "config.json").is_file() and (bundle_path / "history_controller.jsonl").is_file():
        return bundle_path
    controller_candidate = bundle_path / "controller"
    if (controller_candidate / "config.json").is_file() and (
        controller_candidate / "history_controller.jsonl"
    ).is_file():
        return controller_candidate
    raise FileNotFoundError(f"unable to locate stub controller bundle at: {bundle_path_raw}")


def _resolve_bundle_root(*, repo_root: Path, bundle_root: str) -> Path:
    raw = bundle_root.strip() if isinstance(bundle_root, str) else ""
    if not raw:
        return (repo_root / "agent" / "experiments").resolve()
    return _resolve_path(raw, repo_root=repo_root)


def _run_stub_replay_only(*, repo_root: Path, replay_only: str) -> int:
    controller_bundle_dir = _resolve_stub_controller_bundle(bundle_path_raw=replay_only, repo_root=repo_root)
    report = _run_stub_replay(controller_bundle_dir=controller_bundle_dir)
    _json_print(report)
    return 0 if report.get("status") == "ok" else 1


def _run_stub_replay(*, controller_bundle_dir: Path) -> dict[str, Any]:
    config_path = controller_bundle_dir / "config.json"
    plan_path = controller_bundle_dir / "plan.json"
    iteration_path = controller_bundle_dir / "iterations" / "iter_0.json"
    if not config_path.exists():
        raise FileNotFoundError(f"missing stub bundle config: {config_path}")
    if not plan_path.exists():
        raise FileNotFoundError(f"missing stub bundle plan: {plan_path}")
    if not iteration_path.exists():
        raise FileNotFoundError(f"missing stub iteration snapshot: {iteration_path}")

    config_payload = _json_load(config_path)
    iteration_payload = _json_load(iteration_path)
    plan = _load_stub_plan(plan_path)
    domain = str(config_payload.get("domain", "stub")).strip().lower() or "stub"
    tool_backend = str(config_payload.get("tool_backend", "stub")).strip().lower() or "stub"
    max_tool_calls_raw = config_payload.get("max_tool_calls")
    if isinstance(max_tool_calls_raw, int) and not isinstance(max_tool_calls_raw, bool):
        max_tool_calls = max_tool_calls_raw
    else:
        max_tool_calls = len(plan.steps)

    replay_artifacts_dir = controller_bundle_dir / "replay" / "strict" / "iter_0" / "artifacts"
    execution = execute_plan(
        plan=plan,
        tool_registry=tool_registry_for_domain(domain=domain, tool_backend=tool_backend),
        budgets={"max_tool_calls": max_tool_calls},
        artifact_store=replay_artifacts_dir,
        continue_on_error=True,
    )

    stored_results_raw = iteration_payload.get("tool_results", [])
    stored_results = [dict(item) for item in stored_results_raw if isinstance(item, Mapping)]
    replay_results_raw = execution.get("tool_results", [])
    replay_results = [dict(item) for item in replay_results_raw if isinstance(item, Mapping)]

    stored_norm = _normalize_for_compare(stored_results)
    replay_norm = _normalize_for_compare(replay_results)
    diff = _first_diff(stored_norm, replay_norm, path="/tool_results")
    stored_metric_scalar = _extract_metric_scalar(stored_results)
    replay_metric_scalar = _extract_metric_scalar(replay_results)
    if diff is None and stored_metric_scalar != replay_metric_scalar:
        diff = (
            "metric scalar mismatch: "
            f"stored={stored_metric_scalar!r} replay={replay_metric_scalar!r}"
        )

    report_path = controller_bundle_dir / "replay" / "strict" / "report.json"
    report_payload = {
        "status": "ok" if diff is None else "mismatch",
        "domain": "stub",
        "replay_mode": "strict",
        "experiment_id": config_payload.get("experiment_id"),
        "controller_run_id": config_payload.get("controller_run_id"),
        "tool_backend": tool_backend,
        "plan_id": execution.get("plan_id"),
        "iterations_checked": 1,
        "steps_checked": len(replay_results),
        "metric_scalar_expected": stored_metric_scalar,
        "metric_scalar_replay": replay_metric_scalar,
        "bundle_path": str(controller_bundle_dir),
        "report_path": str(report_path),
        "error": diff,
        "timestamp_utc": utc_now(),
    }
    _json_write(report_path, report_payload)
    return report_payload


def _run_stub_mode(
    *,
    repo_root: Path,
    config_path: str,
    plan_path: str,
    bundle_root: str,
    acceptance_replay_strict: bool,
    replay_only: str,
) -> int:
    if replay_only.strip():
        if plan_path.strip():
            raise ValueError("--plan cannot be used with --replay_only in --domain stub mode")
        return _run_stub_replay_only(repo_root=repo_root, replay_only=replay_only)

    if not config_path.strip():
        raise ValueError("--config is required in --domain stub mode")
    if not plan_path.strip():
        raise ValueError("--plan is required in --domain stub mode (unless --replay_only is used)")

    config_abs = _resolve_path(config_path, repo_root=repo_root)
    plan_abs = _resolve_path(plan_path, repo_root=repo_root)
    if not config_abs.exists():
        raise FileNotFoundError(f"stub config not found: {config_abs}")
    if not plan_abs.exists():
        raise FileNotFoundError(f"stub plan not found: {plan_abs}")

    stub_config = _load_stub_config(config_abs)
    plan = _load_stub_plan(plan_abs)
    plan_id = compute_plan_id(plan)

    experiment_id = stub_config.experiment_id or f"stub_{plan_id[:12]}"
    controller_run_id = stub_config.controller_run_id or f"{experiment_id}_run"
    bundle_root_abs = _resolve_bundle_root(repo_root=repo_root, bundle_root=bundle_root)
    experiment_bundle_dir = bundle_root_abs / experiment_id
    controller_bundle_dir = experiment_bundle_dir / "controller"
    shutil.rmtree(controller_bundle_dir, ignore_errors=True)
    controller_bundle_dir.mkdir(parents=True, exist_ok=True)

    run_root = (
        _resolve_path(stub_config.run_root, repo_root=repo_root)
        if isinstance(stub_config.run_root, str)
        else (experiment_bundle_dir / "run").resolve()
    )
    history_path = (
        _resolve_path(stub_config.history_path, repo_root=repo_root)
        if isinstance(stub_config.history_path, str)
        else (controller_bundle_dir / "history_controller.jsonl").resolve()
    )
    if history_path.resolve() != (controller_bundle_dir / "history_controller.jsonl").resolve():
        raise ValueError("stub mode requires history_path to resolve to <bundle>/controller/history_controller.jsonl")
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text("", encoding="utf-8")

    iteration_artifacts_dir = controller_bundle_dir / "iterations" / "iter_0" / "artifacts"
    max_tool_calls = stub_config.max_tool_calls or len(plan.steps)
    start_time = time.monotonic()
    execution = execute_plan(
        plan=plan,
        tool_registry=tool_registry_for_domain(domain="stub", tool_backend=stub_config.tool_backend),
        budgets={"max_tool_calls": max_tool_calls},
        artifact_store=iteration_artifacts_dir,
        continue_on_error=True,
    )
    elapsed_sec = max(0.0, float(time.monotonic() - start_time))

    tool_results_raw = execution.get("tool_results", [])
    tool_results = [dict(item) for item in tool_results_raw if isinstance(item, Mapping)]
    steps_executed = int(execution.get("steps_executed", len(tool_results)) or 0)
    success = all(item.get("status") == "success" for item in tool_results) if tool_results else True
    iteration_status = "success" if success else "failed"
    termination_reason = plan.termination_reason or ("plan_complete" if success else "tool_error")
    run_id = _infer_run_id(tool_results)
    metric_scalar = _extract_metric_scalar(tool_results)
    error = _first_tool_error(tool_results)

    summary_payload = {
        "version": "stub_summary_v1",
        "domain": "stub",
        "plan_id": execution.get("plan_id"),
        "seed": stub_config.seed,
        "should_stop": bool(plan.should_stop),
        "steps_total": len(plan.steps),
        "steps_executed": steps_executed,
        "successful_steps": sum(1 for item in tool_results if item.get("status") == "success"),
        "metric_scalar": metric_scalar,
    }

    history_writer = HistoryWriter(history_path)
    iteration_record = HistoryRecordV0(
        schema_version="v0",
        record_type="iteration",
        timestamp_utc=utc_now(),
        controller_run_id=controller_run_id,
        iteration=0,
        status=iteration_status,
        run_id=run_id,
        summary=summary_payload,
        plan=plan.to_dict(),
        tool_results=tool_results,
        artifact_paths={
            "plan_path": str(controller_bundle_dir / "plan.json"),
            "tool_results_path": str(iteration_artifacts_dir),
        },
        budgets=_budget_snapshot(
            max_tool_calls=max_tool_calls,
            tool_calls_used=steps_executed,
            iterations_completed=0,
            elapsed_sec=elapsed_sec,
        ),
        termination_reason=termination_reason,
        error=error,
    )
    stored_iteration_record = history_writer.append(iteration_record)

    run_end_status = "terminated" if plan.should_stop and success else iteration_status
    run_end_record = HistoryRecordV0(
        schema_version="v0",
        record_type="run_end",
        timestamp_utc=utc_now(),
        controller_run_id=controller_run_id,
        iteration=None,
        status=run_end_status,
        run_id=None,
        summary=None,
        plan=None,
        tool_results=[],
        artifact_paths={"run_dir": str(run_root)},
        budgets=_budget_snapshot(
            max_tool_calls=max_tool_calls,
            tool_calls_used=steps_executed,
            iterations_completed=1,
            elapsed_sec=elapsed_sec,
        ),
        termination_reason=termination_reason,
        error=error,
    )
    history_writer.append(run_end_record)

    git_sha = _git_head_sha(repo_root)
    config_effective_payload = {
        "domain": "stub",
        "config_path": str(config_abs),
        "plan_path": str(plan_abs),
        "toolpack": {"name": stub_config.toolpack_name, "backend": stub_config.tool_backend},
        "seed": stub_config.seed,
        "max_tool_calls": max_tool_calls,
        "config": stub_config.raw_config,
        "runner_version": RUNNER_VERSION,
    }
    config_effective_path = controller_bundle_dir / "config_effective.yaml"
    _yaml_write(config_effective_path, config_effective_payload)

    config_payload = {
        "experiment_id": experiment_id,
        "controller_run_id": controller_run_id,
        "domain": "stub",
        "toolpack_name": stub_config.toolpack_name,
        "tool_backend": stub_config.tool_backend,
        "seed": stub_config.seed,
        "max_tool_calls": max_tool_calls,
        "run_dir": str(run_root),
        "history_path": str(history_path),
        "plan_path": "plan.json",
        "plan_id": execution.get("plan_id"),
        "head_sha": git_sha,
        "HEAD_SHA": git_sha,
        "controller_version": RUNNER_VERSION,
        "acceptance_replay_strict": bool(acceptance_replay_strict),
        "cli_invocation": _cli_invocation(argv=sys.argv, module_name="kernel.runner"),
        "config_effective_path": "config_effective.yaml",
    }
    _json_write(controller_bundle_dir / "config.json", config_payload)
    _json_write(controller_bundle_dir / "plan.json", plan.to_dict())
    _yaml_write(controller_bundle_dir / "param_space.yaml", stub_config.param_space_payload)

    iteration_snapshot_path = controller_bundle_dir / "iterations" / "iter_0.json"
    iteration_snapshot_payload = {
        "iteration": 0,
        "status": iteration_status,
        "RUN_ID": run_id,
        "summary": summary_payload,
        "plan": plan.to_dict(),
        "tool_results": tool_results,
        "termination_reason": termination_reason,
        "error": error,
    }
    _json_write(iteration_snapshot_path, iteration_snapshot_payload)

    replay_report = _run_stub_replay(controller_bundle_dir=controller_bundle_dir)
    replay_ok = replay_report.get("status") == "ok"

    summary_file_payload = {
        "experiment_id": experiment_id,
        "controller_run_id": controller_run_id,
        "iterations_total": 1,
        "termination_reason": termination_reason,
        "run_dir": str(run_root),
        "domain": "stub",
        "tool_backend": stub_config.tool_backend,
        "seed": stub_config.seed,
        "plan_id": execution.get("plan_id"),
        "steps_executed": steps_executed,
        "metric_scalar": metric_scalar,
        "head_sha": git_sha,
        "HEAD_SHA": git_sha,
        "controller_version": RUNNER_VERSION,
        "acceptance_replay_strict": bool(acceptance_replay_strict),
        "strict_replay_status": replay_report.get("status"),
        "bundle_files": {
            "config": "config.json",
            "config_effective": "config_effective.yaml",
            "history": "history_controller.jsonl",
            "iterations": "iterations/",
            "param_space": "param_space.yaml",
            "plan": "plan.json",
            "summary": "summary.json",
            "replay_report": "replay/strict/report.json",
        },
    }
    _json_write(controller_bundle_dir / "summary.json", summary_file_payload)

    result_payload = {
        "status": "ok" if success and replay_ok else "failed",
        "domain": "stub",
        "experiment_id": experiment_id,
        "controller_run_id": controller_run_id,
        "experiment_bundle_path": str(experiment_bundle_dir),
        "controller_bundle_path": str(controller_bundle_dir),
        "iteration_status": stored_iteration_record.get("status"),
        "plan_id": execution.get("plan_id"),
        "steps_executed": steps_executed,
        "metric_scalar": metric_scalar,
        "acceptance_replay_strict": bool(acceptance_replay_strict),
        "strict_replay": replay_report,
        "strict_replay_status": replay_report.get("status"),
        "error": error,
    }
    _json_print(result_payload)

    if not success:
        return 1
    if not replay_ok:
        return 1
    return 0


def _run_cholla_delegate(
    *,
    config_path: str,
    acceptance_replay_strict: bool,
    replay_only: str,
) -> int:
    from agent.controller import run_phase3c

    argv: list[str] = []
    config_norm = config_path.strip()
    replay_norm = replay_only.strip()
    if bool(config_norm) == bool(replay_norm):
        raise ValueError("domain=cholla requires exactly one of config_path or replay_only")

    if config_norm:
        argv.extend(["--config", config_norm])
    if replay_norm:
        argv.extend(["--replay_only", replay_norm])
    if acceptance_replay_strict:
        argv.append("--acceptance_replay_strict")
    return int(run_phase3c.main(argv))


def run(
    *,
    domain: str,
    config_path: str = "",
    plan_path: str = "",
    bundle_root: str = "",
    acceptance_replay_strict: bool = False,
    replay_only: str = "",
) -> int:
    """Run kernel execution in cholla or stub domain."""

    repo_root = _repo_root()
    domain_norm = _as_string(domain, label="domain").lower()
    if domain_norm not in {"cholla", "stub"}:
        raise ValueError("domain must be one of: cholla, stub")

    if domain_norm == "cholla":
        return _run_cholla_delegate(
            config_path=config_path,
            acceptance_replay_strict=acceptance_replay_strict,
            replay_only=replay_only,
        )

    return _run_stub_mode(
        repo_root=repo_root,
        config_path=config_path,
        plan_path=plan_path,
        bundle_root=bundle_root,
        acceptance_replay_strict=acceptance_replay_strict,
        replay_only=replay_only,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Domain-agnostic kernel PlanSpec runner.")
    parser.add_argument(
        "--domain",
        type=str,
        required=True,
        choices=["cholla", "stub"],
        help="Execution domain. cholla delegates to phase3c runner; stub executes PlanSpec directly.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="Config path. Required for domain=cholla and domain=stub run mode.",
    )
    parser.add_argument(
        "--plan",
        type=str,
        default="",
        help="PlanSpec JSON path. Required for domain=stub run mode.",
    )
    parser.add_argument(
        "--bundle-root",
        type=str,
        default="agent/experiments",
        help="Output bundle root for domain=stub.",
    )
    parser.add_argument(
        "--acceptance_replay_strict",
        action="store_true",
        help="Require strict replay success after run mode completion.",
    )
    parser.add_argument(
        "--replay_only",
        type=str,
        default="",
        help="Replay-only mode from an existing bundle path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        return run(
            domain=args.domain,
            config_path=args.config,
            plan_path=args.plan,
            bundle_root=args.bundle_root,
            acceptance_replay_strict=bool(args.acceptance_replay_strict),
            replay_only=args.replay_only,
        )
    except Exception as exc:  # noqa: BLE001
        _json_print(
            {
                "status": "failed",
                "domain": args.domain,
                "error": str(exc),
                "mode": "replay_only" if args.replay_only.strip() else "run",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
