"""Kernel plan executor with deterministic step-order tool invocation."""

from __future__ import annotations

from copy import deepcopy
import importlib
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping

from kernel.planspec import (
    PLAN_SPEC_SCHEMA_VERSION,
    STEP_SPEC_SCHEMA_VERSION,
    PlanSpec,
    StepSpec,
    canonical_json,
    compute_plan_id,
    compute_step_id,
)


ToolRegistry = Mapping[str, Mapping[str, Any]]


def _load_execute_run_module():
    agent_dir = Path(__file__).resolve().parents[1] / "agent"
    agent_dir_str = str(agent_dir)
    if agent_dir_str not in sys.path:
        sys.path.insert(0, agent_dir_str)
    return importlib.import_module("agent.execute_run")


def _resolve_tool_callable(callable_ref: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if callable(callable_ref):
        return callable_ref

    if not isinstance(callable_ref, str) or ":" not in callable_ref:
        raise TypeError(f"invalid tool callable reference: {callable_ref!r}")

    module_name, attr_name = callable_ref.split(":", 1)
    if not module_name or not attr_name:
        raise TypeError(f"invalid tool callable reference: {callable_ref!r}")

    module = importlib.import_module(module_name)
    fn = getattr(module, attr_name, None)
    if not callable(fn):
        raise TypeError(f"resolved tool callable is not callable: {callable_ref!r}")
    return fn


def _tool_call_status(*, tool_name: str, result: Mapping[str, Any]) -> str:
    if tool_name == "validate_params":
        return "success" if result.get("valid") is True else "failed"
    if tool_name == "run_cholla":
        return "success" if result.get("status") == "success" else "failed"
    if tool_name in {"compute_metric", "compute_metric_v3"}:
        details = result.get("details")
        details_map = dict(details) if isinstance(details, Mapping) else {}
        return "success" if details_map.get("status") == "success" else "failed"

    status = result.get("status")
    if isinstance(status, str) and status.lower() in {"success", "ok", "valid"}:
        return "success"
    if isinstance(status, str):
        return "failed"
    return "success"


def _invoke_tool(*, tool_name: str, payload: dict[str, Any], tool_registry: ToolRegistry) -> dict[str, Any]:
    descriptor = tool_registry.get(tool_name)
    if descriptor is None:
        raise KeyError(f"unknown tool: {tool_name}")

    tool_fn = _resolve_tool_callable(descriptor.get("callable"))
    result = tool_fn(payload)
    if not isinstance(result, dict):
        raise TypeError(f"tool {tool_name!r} returned non-object payload: {type(result).__name__}")
    return result


def _coerce_step(step: StepSpec | Mapping[str, Any]) -> StepSpec:
    if isinstance(step, StepSpec):
        return step
    if not isinstance(step, Mapping):
        raise TypeError(f"step must be StepSpec or mapping, got {type(step).__name__}")

    tool = step.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise TypeError("step.tool must be a non-empty string")
    payload = step.get("payload")
    if not isinstance(payload, Mapping):
        raise TypeError("step.payload must be an object")

    expected_artifacts_raw = step.get("expected_artifacts", [])
    if not isinstance(expected_artifacts_raw, list):
        raise TypeError("step.expected_artifacts must be a list")

    metadata_raw = step.get("metadata", {})
    if not isinstance(metadata_raw, Mapping):
        raise TypeError("step.metadata must be an object")

    note = step.get("note")
    if note is not None and not isinstance(note, str):
        raise TypeError("step.note must be a string or null")

    return StepSpec(
        tool=tool,
        payload=dict(payload),
        note=note,
        expected_artifacts=[str(item) for item in expected_artifacts_raw],
        expects_run_id=bool(step.get("expects_run_id", False)),
        expects_metric_scalar=bool(step.get("expects_metric_scalar", False)),
        metadata=dict(metadata_raw),
        schema_version=str(step.get("schema_version", STEP_SPEC_SCHEMA_VERSION)),
    )


def _coerce_plan(plan: PlanSpec | Mapping[str, Any]) -> PlanSpec:
    if isinstance(plan, PlanSpec):
        return plan
    if not isinstance(plan, Mapping):
        raise TypeError(f"plan must be PlanSpec or mapping, got {type(plan).__name__}")

    steps_raw = plan.get("steps", [])
    if not isinstance(steps_raw, list):
        raise TypeError("plan.steps must be a list")

    metadata_raw = plan.get("metadata", {})
    if not isinstance(metadata_raw, Mapping):
        raise TypeError("plan.metadata must be an object")

    rationale = plan.get("rationale", "")
    if not isinstance(rationale, str):
        raise TypeError("plan.rationale must be a string")

    termination_reason = plan.get("termination_reason")
    if termination_reason is not None and not isinstance(termination_reason, str):
        raise TypeError("plan.termination_reason must be a string or null")

    return PlanSpec(
        steps=[_coerce_step(step) for step in steps_raw],
        should_stop=bool(plan.get("should_stop", False)),
        termination_reason=termination_reason,
        rationale=rationale,
        metadata=dict(metadata_raw),
        schema_version=str(plan.get("schema_version", PLAN_SPEC_SCHEMA_VERSION)),
    )


def _write_tool_call_artifact(
    *,
    artifact_store: Any,
    call_index: int,
    tool_name: str,
    payload: Mapping[str, Any],
) -> str | None:
    if artifact_store is None:
        return None

    file_name = f"tool_{call_index:02d}_{_safe_tool_name(tool_name)}.json"
    text = canonical_json(dict(payload)) + "\n"

    writer = getattr(artifact_store, "write_json", None)
    if callable(writer):
        out_path = writer(file_name, dict(payload))
        if isinstance(out_path, (Path, str)):
            return str(out_path)
        return file_name

    if isinstance(artifact_store, (str, Path)):
        root = Path(artifact_store)
        root.mkdir(parents=True, exist_ok=True)
        out_path = (root / file_name).resolve()
        out_path.write_text(text, encoding="utf-8")
        return str(out_path)

    raise TypeError(
        "artifact_store must be None, a directory path, or expose write_json(path, payload)"
    )


def _safe_tool_name(tool_name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name.strip())
    return sanitized or "tool"


def render_params(template_text: str, overrides: dict[str, Any]) -> str:
    """Compatibility pass-through for params rendering."""

    module = _load_execute_run_module()
    return module._render_params(template_text, overrides)


def execute_run(
    *,
    repo_root: Path,
    seed: int,
    iteration: int,
    agent_run_id: str,
    spec_path: Path,
    template_params_path: Path,
    template_schedule_path: Path,
) -> dict[str, Any]:
    """Compatibility pass-through for one iteration execution."""

    module = _load_execute_run_module()
    return module.execute_run(
        repo_root=repo_root,
        seed=seed,
        iteration=iteration,
        agent_run_id=agent_run_id,
        spec_path=spec_path,
        template_params_path=template_params_path,
        template_schedule_path=template_schedule_path,
    )


def execute_plan(
    plan: PlanSpec | Mapping[str, Any],
    tool_registry: ToolRegistry,
    budgets: Mapping[str, Any] | None = None,
    artifact_store: Any = None,
    *,
    continue_on_error: bool = True,
) -> dict[str, Any]:
    """Execute one PlanSpec deterministically and return step-aligned tool results.

    Tool invocation path mirrors the controller:
    descriptor -> callable resolution -> tool_fn(payload).
    """

    normalized_plan = _coerce_plan(plan)
    plan_id = compute_plan_id(normalized_plan)
    tool_results: list[dict[str, Any]] = []

    for idx, step in enumerate(normalized_plan.steps):
        step_id = compute_step_id(plan_id, idx, step)
        tool_input = deepcopy(step.payload)

        try:
            result = _invoke_tool(tool_name=step.tool, payload=tool_input, tool_registry=tool_registry)
        except Exception as exc:  # noqa: BLE001
            result = {"status": "error", "error": str(exc)}

        status = _tool_call_status(tool_name=step.tool, result=result)
        call_record: dict[str, Any] = {
            "index": idx,
            "plan_id": plan_id,
            "step_id": step_id,
            "tool": step.tool,
            "status": status,
            "payload": deepcopy(tool_input),
            "result": deepcopy(result),
        }

        artifact_path = _write_tool_call_artifact(
            artifact_store=artifact_store,
            call_index=idx,
            tool_name=step.tool,
            payload=call_record,
        )
        if artifact_path is not None:
            call_record["artifact_path"] = artifact_path

        tool_results.append(call_record)
        if not continue_on_error and status != "success":
            break

    return {
        "plan_id": plan_id,
        "tool_results": tool_results,
        "steps_executed": len(tool_results),
        "budgets": dict(budgets) if isinstance(budgets, Mapping) else {},
    }


__all__ = ["execute_run", "execute_plan", "render_params"]
