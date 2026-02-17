"""Kernel controller wiring for M1 PlanSpec + PlanExecutor execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from agent.controller.controller import HybridController as _AgentHybridController
from kernel.plan_executor import execute_plan
from kernel.plans.cholla_chain import build_cholla_plan
from kernel.planspec import PlanSpec, StepSpec


class HybridController(_AgentHybridController):
    """Hybrid controller wired to invoke tools via kernel PlanExecutor."""

    def _invoke_tool(self, *, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = StepSpec(tool=tool_name, payload=dict(payload))
        plan = PlanSpec(steps=[step], should_stop=False)
        execution = execute_plan(
            plan=plan,
            tool_registry=self.tool_registry,
            budgets=None,
            artifact_store=None,
            continue_on_error=False,
        )
        tool_results = execution.get("tool_results")
        if not isinstance(tool_results, list) or not tool_results:
            raise RuntimeError("plan executor returned no tool_results")

        first = tool_results[0]
        if not isinstance(first, Mapping):
            raise TypeError(f"tool result entry must be an object, got {type(first).__name__}")
        result = first.get("result")
        if not isinstance(result, Mapping):
            raise TypeError(f"tool {tool_name!r} returned non-object payload: {type(result).__name__}")
        return dict(result)

    def _execute_iteration_tool_chain(
        self,
        *,
        iteration: int,
        proposed_params: dict[str, Any],
        template_params_text: str,
        schedule_text: str,
        iter_dir: Path,
        artifacts_dir: Path,
        start_time: float,
        tool_calls_used: int,
    ) -> tuple[list[dict[str, Any]], dict[str, str], int, str | None, str | None]:
        tool_results: list[dict[str, Any]] = []
        artifact_paths: dict[str, str] = {}
        call_index = 0

        planned_chain = build_cholla_plan(
            iteration=iteration,
            controller_run_id=self.controller_run_id,
            proposed_params=proposed_params,
            template_params_text=template_params_text,
            schedule_text=schedule_text,
            iter_dir=iter_dir,
            run_manifest_path="",
            rationale="fixed_cholla_chain",
        )

        validate_payload = dict(planned_chain.steps[0].payload)
        validate_call, validate_path, tool_calls_used, budget_reason = self._invoke_and_record_tool_call(
            call_index=call_index,
            tool_name="validate_params",
            payload=validate_payload,
            artifacts_dir=artifacts_dir,
            start_time=start_time,
            tool_calls_used=tool_calls_used,
        )
        if budget_reason:
            return (tool_results, artifact_paths, tool_calls_used, None, budget_reason)
        tool_results.append(validate_call)
        artifact_paths["validate_params_call_path"] = validate_path

        validate_result = validate_call.get("result")
        validate_result_map = dict(validate_result) if isinstance(validate_result, Mapping) else {}
        if validate_call.get("status") != "success" or validate_result_map.get("valid") is not True:
            errors = validate_result_map.get("errors")
            return (
                tool_results,
                artifact_paths,
                tool_calls_used,
                f"validate_params_failed: {errors}",
                "tool_error",
            )

        run_payload = dict(planned_chain.steps[1].payload)
        params_text = str(run_payload.get("params_text", ""))
        rendered_params_path = artifacts_dir / "rendered_params.txt"
        rendered_schedule_path = artifacts_dir / "rendered_schedule.txt"
        rendered_params_path.write_text(params_text, encoding="utf-8")
        rendered_schedule_path.write_text(schedule_text, encoding="utf-8")
        artifact_paths["rendered_params_path"] = str(rendered_params_path)
        artifact_paths["rendered_schedule_path"] = str(rendered_schedule_path)

        call_index += 1
        run_call, run_path, tool_calls_used, budget_reason = self._invoke_and_record_tool_call(
            call_index=call_index,
            tool_name="run_cholla",
            payload=run_payload,
            artifacts_dir=artifacts_dir,
            start_time=start_time,
            tool_calls_used=tool_calls_used,
        )
        if budget_reason:
            return (tool_results, artifact_paths, tool_calls_used, None, budget_reason)
        tool_results.append(run_call)
        artifact_paths["run_cholla_call_path"] = run_path

        run_result = run_call.get("result")
        run_result_map = dict(run_result) if isinstance(run_result, Mapping) else {}
        if run_call.get("status") != "success":
            return (
                tool_results,
                artifact_paths,
                tool_calls_used,
                f"run_cholla_failed: {run_result_map.get('error')}",
                "tool_error",
            )
        run_manifest_path = run_result_map.get("run_manifest_path")
        if not isinstance(run_manifest_path, str) or not run_manifest_path.strip():
            return (
                tool_results,
                artifact_paths,
                tool_calls_used,
                "run_cholla_failed: missing run_manifest_path",
                "tool_error",
            )

        compute_chain = build_cholla_plan(
            iteration=iteration,
            controller_run_id=self.controller_run_id,
            proposed_params=proposed_params,
            template_params_text=template_params_text,
            schedule_text=schedule_text,
            iter_dir=iter_dir,
            run_manifest_path=run_manifest_path,
            rationale="fixed_cholla_chain",
        )
        compute_payload = dict(compute_chain.steps[2].payload)
        call_index += 1
        metric_call, metric_path, tool_calls_used, budget_reason = self._invoke_and_record_tool_call(
            call_index=call_index,
            tool_name="compute_metric",
            payload=compute_payload,
            artifacts_dir=artifacts_dir,
            start_time=start_time,
            tool_calls_used=tool_calls_used,
        )
        if budget_reason:
            return (tool_results, artifact_paths, tool_calls_used, None, budget_reason)
        tool_results.append(metric_call)
        artifact_paths["compute_metric_call_path"] = metric_path

        metric_result = metric_call.get("result")
        metric_result_map = dict(metric_result) if isinstance(metric_result, Mapping) else {}
        metric_details = metric_result_map.get("details")
        metric_details_map = dict(metric_details) if isinstance(metric_details, Mapping) else {}
        metric_status = metric_details_map.get("status")
        if metric_call.get("status") != "success" or metric_status != "success":
            metric_error = metric_details_map.get("error")
            return (
                tool_results,
                artifact_paths,
                tool_calls_used,
                f"compute_metric_failed: {metric_error}",
                "tool_error",
            )

        return (tool_results, artifact_paths, tool_calls_used, None, None)


__all__ = ["HybridController"]
