"""Build PlanSpec objects equivalent to the current fixed Cholla tool chain."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from kernel.planspec import PlanSpec, StepSpec, compute_plan_id, compute_step_id


def _format_param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def render_params_text(template_text: str, overrides: Mapping[str, Any]) -> str:
    """Render params text exactly like the current controller tool-chain path."""

    rendered_lines: list[str] = []
    remaining = {str(key): _format_param_value(value) for key, value in overrides.items()}

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
        rendered_lines.append("# Added by hybrid controller")
        for key in sorted(remaining):
            rendered_lines.append(f"{key}={remaining[key]}")

    return "\n".join(rendered_lines) + "\n"


def build_cholla_tool_inputs(
    *,
    iteration: int,
    controller_run_id: str,
    proposed_params: Mapping[str, Any],
    template_params_text: str,
    schedule_text: str,
    iter_dir: Path | str,
    run_manifest_path: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build the exact tool payload dicts used by the current fixed chain."""

    params_text = render_params_text(template_params_text, proposed_params)
    resolved_iter_dir = Path(iter_dir)

    validate_payload = {"params": dict(proposed_params)}
    run_payload = {
        "params_text": params_text,
        "schedule_text": schedule_text,
        "out_root": str((resolved_iter_dir / "backend_runs").resolve()),
        "run_id": f"{controller_run_id}_iter_{iteration:04d}",
    }
    compute_payload = {"run_manifest_path": run_manifest_path}
    return (validate_payload, run_payload, compute_payload)


def build_cholla_plan(
    *,
    iteration: int,
    controller_run_id: str,
    proposed_params: Mapping[str, Any],
    template_params_text: str,
    schedule_text: str,
    iter_dir: Path | str,
    run_manifest_path: str,
    rationale: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> PlanSpec:
    """Build a deterministic 3-step PlanSpec for validate->run->metric."""

    validate_payload, run_payload, compute_payload = build_cholla_tool_inputs(
        iteration=iteration,
        controller_run_id=controller_run_id,
        proposed_params=proposed_params,
        template_params_text=template_params_text,
        schedule_text=schedule_text,
        iter_dir=iter_dir,
        run_manifest_path=run_manifest_path,
    )

    base_plan = PlanSpec(
        steps=[
            StepSpec(tool="validate_params", payload=validate_payload),
            StepSpec(tool="run_cholla", payload=run_payload, expects_run_id=True),
            StepSpec(tool="compute_metric", payload=compute_payload, expects_metric_scalar=True),
        ],
        should_stop=False,
        termination_reason=None,
        rationale=rationale,
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )

    return _attach_plan_and_step_ids(base_plan)


def _attach_plan_and_step_ids(plan: PlanSpec) -> PlanSpec:
    plan_id = compute_plan_id(plan)
    step_ids = [compute_step_id(plan_id, idx, step) for idx, step in enumerate(plan.steps)]

    steps_with_ids: list[StepSpec] = []
    for idx, step in enumerate(plan.steps):
        step_metadata = dict(step.metadata)
        step_metadata["step_id"] = step_ids[idx]
        steps_with_ids.append(
            StepSpec(
                tool=step.tool,
                payload=dict(step.payload),
                note=step.note,
                expected_artifacts=list(step.expected_artifacts),
                expects_run_id=step.expects_run_id,
                expects_metric_scalar=step.expects_metric_scalar,
                metadata=step_metadata,
                schema_version=step.schema_version,
            )
        )

    plan_metadata = dict(plan.metadata)
    plan_metadata["plan_id"] = plan_id
    plan_metadata["step_ids"] = list(step_ids)

    return PlanSpec(
        steps=steps_with_ids,
        should_stop=plan.should_stop,
        termination_reason=plan.termination_reason,
        rationale=plan.rationale,
        metadata=plan_metadata,
        schema_version=plan.schema_version,
    )


__all__ = ["build_cholla_plan", "build_cholla_tool_inputs", "render_params_text"]
