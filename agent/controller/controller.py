"""Hybrid controller scaffold for advisory planning over Tier-4 tools."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Mapping

import yaml

from agent.controller.history import HistoryRecordV0, HistoryWriter, utc_now
from agent.controller.specs import PlanSpecV0, SummarySpecV0, validate_plan_spec, validate_summary_spec
from agent.tools.registry import TOOLS as DEFAULT_TOOLS


def _resolve_tool_callable(callable_ref: Any) -> Any:
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


def _write_json(path: Path, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object at {path}")
    return payload


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{lineno} expected JSON object")
        records.append(payload)
    return records


def _jsonl_text(records: list[dict[str, Any]]) -> str:
    if not records:
        return ""
    return "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n"


def _derive_controller_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"hybrid_{stamp}"


def _format_param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _render_params_text(template_text: str, overrides: Mapping[str, Any]) -> str:
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


def _first_diff(expected: Any, observed: Any, *, path: str = "$") -> str:
    if type(expected) is not type(observed):  # noqa: E721
        return (
            f"{path}: type mismatch expected {type(expected).__name__}, "
            f"got {type(observed).__name__}"
        )

    if isinstance(expected, dict):
        expected_keys = sorted(expected.keys())
        observed_keys = sorted(observed.keys())
        if expected_keys != observed_keys:
            missing = sorted(set(expected_keys) - set(observed_keys))
            extra = sorted(set(observed_keys) - set(expected_keys))
            if missing:
                return f"{path}: missing keys {missing}"
            return f"{path}: unexpected keys {extra}"
        for key in expected_keys:
            nested = _first_diff(expected[key], observed[key], path=f"{path}.{key}")
            if nested:
                return nested
        return ""

    if isinstance(expected, list):
        if len(expected) != len(observed):
            return f"{path}: length mismatch expected {len(expected)}, got {len(observed)}"
        for idx, (e_item, o_item) in enumerate(zip(expected, observed)):
            nested = _first_diff(e_item, o_item, path=f"{path}[{idx}]")
            if nested:
                return nested
        return ""

    if expected != observed:
        return f"{path}: value mismatch expected {expected!r}, got {observed!r}"
    return ""


class HybridController:
    """Orchestrate summarizer/planner advisory calls and Tier-4 tool execution."""

    def __init__(
        self,
        *,
        repo_root: Path | str,
        summarizer: Any,
        planner: Any,
        param_space_path: Path | str = Path("agent/spec/param_space_v0.yaml"),
        history_path: Path | str = Path("agent/history/history.jsonl"),
        run_root: Path | str = Path("agent/runs"),
        tool_registry: Mapping[str, Mapping[str, Any]] | None = None,
        controller_run_id: str = "",
        experiment_id: str = "",
        max_iterations: int = 1,
        max_tool_calls: int = 4,
        walltime_budget_sec: float | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.summarizer = summarizer
        self.planner = planner
        self.param_space_path = self._resolve_path(Path(param_space_path))
        self.run_root = self._resolve_path(Path(run_root))
        self.history_writer = HistoryWriter(self._resolve_path(Path(history_path)))
        self.tool_registry: dict[str, Mapping[str, Any]] = (
            dict(tool_registry) if tool_registry is not None else dict(DEFAULT_TOOLS)
        )
        self.controller_run_id = controller_run_id.strip() or _derive_controller_run_id()
        self.experiment_id = experiment_id.strip() or f"ctrl_{self.controller_run_id}"
        self.max_iterations = int(max_iterations)
        self.max_tool_calls = int(max_tool_calls)
        self.walltime_budget_sec = float(walltime_budget_sec) if walltime_budget_sec is not None else None

        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be >= 0")
        if self.walltime_budget_sec is not None and self.walltime_budget_sec < 0:
            raise ValueError("walltime_budget_sec must be >= 0 when provided")

    def _resolve_path(self, path: Path) -> Path:
        if path.is_absolute():
            return path.resolve()
        return (self.repo_root / path).resolve()

    def load_param_space(self) -> dict[str, Any]:
        """Load and validate the param-space YAML payload."""

        payload = yaml.safe_load(self.param_space_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"expected mapping at param space: {self.param_space_path}")
        return payload

    def run(self, initial_state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run the hybrid loop until budget or explicit termination."""

        param_space = self.load_param_space()
        template_params_path, template_schedule_path = self._resolve_template_paths(param_space)
        template_params_text = template_params_path.read_text(encoding="utf-8")
        schedule_text = template_schedule_path.read_text(encoding="utf-8")

        run_dir = self.run_root / self.controller_run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        state: dict[str, Any] = dict(initial_state or {})
        state.setdefault("param_space", param_space)
        state.setdefault("history_path", str(self.history_writer.path))
        if not isinstance(state.get("history_records"), list):
            state["history_records"] = []

        planner_seed = self._planner_seed()
        linked_experiment_id = self._linked_experiment_id(state)
        run_history_records: list[dict[str, Any]] = []

        start_time = time.monotonic()
        tool_calls_used = 0
        iterations_completed = 0
        failed_iterations = 0
        termination_reason = "max_iterations_reached"
        last_error: str | None = None

        for iteration in range(self.max_iterations):
            budget_reason = self._budget_termination(tool_calls_used=tool_calls_used, start_time=start_time)
            if budget_reason:
                termination_reason = budget_reason
                break

            iter_dir = run_dir / f"iter_{iteration}"
            artifacts_dir = iter_dir / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            summary_spec: SummarySpecV0 | None = None
            plan_spec: PlanSpecV0 | None = None
            tool_results: list[dict[str, Any]] = []
            artifact_paths: dict[str, str] = {
                "template_params_path": str(template_params_path),
                "template_schedule_path": str(template_schedule_path),
            }
            iteration_status = "success"
            iteration_error: str | None = None
            iteration_termination: str | None = None

            budgets = self._budget_snapshot(
                iterations_completed=iterations_completed,
                tool_calls_used=tool_calls_used,
                start_time=start_time,
            )

            try:
                summary_input = {
                    "iteration": iteration,
                    "state": state,
                    "budgets": budgets,
                    "param_space": param_space,
                }
                summary_spec = validate_summary_spec(self._invoke_summarizer(summary_input))
                summary_path = artifacts_dir / "summary_spec.json"
                _write_json(summary_path, summary_spec.to_dict())
                artifact_paths["summary_spec_path"] = str(summary_path)
                if summary_spec.state_patch:
                    state.update(summary_spec.state_patch)
            except Exception as exc:  # noqa: BLE001
                iteration_status = "failed"
                iteration_error = f"summarizer_error: {exc}"
                iteration_termination = "summarizer_error"

            if iteration_status == "success" and summary_spec is not None and summary_spec.should_stop:
                iteration_status = "terminated"
                iteration_termination = summary_spec.termination_reason_hint or "summarizer_requested_stop"

            if iteration_status == "success":
                try:
                    planner_input = {
                        "iteration": iteration,
                        "state": state,
                        "budgets": budgets,
                        "summary": summary_spec.to_dict() if summary_spec is not None else {},
                        "param_space": param_space,
                        "history_path": str(self.history_writer.path),
                    }
                    plan_spec = validate_plan_spec(self._invoke_planner(planner_input))
                    plan_path = artifacts_dir / "plan_spec.json"
                    _write_json(plan_path, plan_spec.to_dict())
                    artifact_paths["plan_spec_path"] = str(plan_path)
                except Exception as exc:  # noqa: BLE001
                    iteration_status = "failed"
                    iteration_error = f"planner_error: {exc}"
                    iteration_termination = "planner_error"

            if iteration_status == "success" and plan_spec is not None and plan_spec.should_stop:
                iteration_status = "terminated"
                iteration_termination = plan_spec.termination_reason or "planner_requested_stop"

            if iteration_status == "success" and plan_spec is not None:
                try:
                    proposed_params = self._extract_proposed_params(plan_spec)
                    (
                        chain_results,
                        chain_paths,
                        tool_calls_used,
                        chain_error,
                        chain_termination,
                    ) = self._execute_iteration_tool_chain(
                        iteration=iteration,
                        proposed_params=proposed_params,
                        template_params_text=template_params_text,
                        schedule_text=schedule_text,
                        iter_dir=iter_dir,
                        artifacts_dir=artifacts_dir,
                        start_time=start_time,
                        tool_calls_used=tool_calls_used,
                    )
                    tool_results.extend(chain_results)
                    artifact_paths.update(chain_paths)
                    if chain_error is not None:
                        iteration_status = "failed"
                        iteration_error = chain_error
                        iteration_termination = chain_termination or "tool_error"
                    elif chain_termination is not None:
                        iteration_status = "terminated"
                        iteration_termination = chain_termination
                except Exception as exc:  # noqa: BLE001
                    iteration_status = "failed"
                    iteration_error = f"tool_error: {exc}"
                    iteration_termination = "tool_error"

            tools_path = artifacts_dir / "tool_results.json"
            _write_json(tools_path, tool_results)
            artifact_paths["tool_results_path"] = str(tools_path)

            iteration_artifact = {
                "controller_run_id": self.controller_run_id,
                "iteration": iteration,
                "status": iteration_status,
                "termination_reason": iteration_termination,
                "error": iteration_error,
                "summary": summary_spec.to_dict() if summary_spec is not None else None,
                "plan": plan_spec.to_dict() if plan_spec is not None else None,
                "tool_results": tool_results,
                "budgets": self._budget_snapshot(
                    iterations_completed=iterations_completed,
                    tool_calls_used=tool_calls_used,
                    start_time=start_time,
                ),
            }
            iteration_path = artifacts_dir / "controller_iteration.json"
            _write_json(iteration_path, iteration_artifact)
            artifact_paths["controller_iteration_path"] = str(iteration_path)

            history_record = HistoryRecordV0(
                schema_version="v0",
                record_type="iteration",
                timestamp_utc=utc_now(),
                controller_run_id=self.controller_run_id,
                iteration=iteration,
                status=iteration_status,
                run_id=self._run_id_from_tool_results(tool_results),
                summary=summary_spec.to_dict() if summary_spec is not None else None,
                plan=plan_spec.to_dict() if plan_spec is not None else None,
                tool_results=tool_results,
                artifact_paths=artifact_paths,
                budgets=self._budget_snapshot(
                    iterations_completed=iterations_completed,
                    tool_calls_used=tool_calls_used,
                    start_time=start_time,
                ),
                termination_reason=iteration_termination,
                error=iteration_error,
            )
            stored_history_record = self.history_writer.append(history_record)
            run_history_records.append(stored_history_record)
            history_records = state.get("history_records")
            if isinstance(history_records, list):
                history_records.append(stored_history_record)

            state["last_iteration"] = iteration
            state["last_summary"] = summary_spec.to_dict() if summary_spec is not None else None
            state["last_plan"] = plan_spec.to_dict() if plan_spec is not None else None
            state["last_tool_results"] = list(tool_results)

            iterations_completed += 1
            if iteration_status == "failed":
                failed_iterations += 1
            if iteration_error is not None:
                last_error = iteration_error

            if iteration_termination:
                termination_reason = iteration_termination
                break
        else:
            termination_reason = "max_iterations_reached"

        run_status = "failed" if failed_iterations > 0 else "success"
        if run_status == "success" and termination_reason != "max_iterations_reached":
            run_status = "terminated"

        run_end_record = HistoryRecordV0(
            schema_version="v0",
            record_type="run_end",
            timestamp_utc=utc_now(),
            controller_run_id=self.controller_run_id,
            iteration=None,
            status=run_status,
            run_id=None,
            summary=None,
            plan=None,
            tool_results=[],
            artifact_paths={"run_dir": str(run_dir)},
            budgets=self._budget_snapshot(
                iterations_completed=iterations_completed,
                tool_calls_used=tool_calls_used,
                start_time=start_time,
            ),
            termination_reason=termination_reason,
            error=last_error,
        )
        stored_run_end_record = self.history_writer.append(run_end_record)
        run_history_records.append(stored_run_end_record)

        bundle_dir = self._write_controller_bundle(
            experiment_id=self.experiment_id,
            run_dir=run_dir,
            param_space=param_space,
            template_params_path=template_params_path,
            template_schedule_path=template_schedule_path,
            history_records=run_history_records,
            seed=planner_seed,
            termination_reason=termination_reason,
            linked_experiment_id=linked_experiment_id,
        )

        return {
            "experiment_id": self.experiment_id,
            "experiment_bundle_path": str(bundle_dir),
            "controller_run_id": self.controller_run_id,
            "run_dir": str(run_dir),
            "history_path": str(self.history_writer.path),
            "iterations_completed": iterations_completed,
            "failed_iterations": failed_iterations,
            "tool_calls_used": tool_calls_used,
            "termination_reason": termination_reason,
        }

    def _invoke_summarizer(self, payload: dict[str, Any]) -> dict[str, Any]:
        if hasattr(self.summarizer, "summarize") and callable(getattr(self.summarizer, "summarize")):
            response = self.summarizer.summarize(payload)
        elif callable(self.summarizer):
            response = self.summarizer(payload)
        else:
            raise TypeError("summarizer must be callable or expose summarize(payload)")
        if not isinstance(response, Mapping):
            raise TypeError(f"summarizer returned non-object payload: {type(response).__name__}")
        return dict(response)

    def _invoke_planner(self, payload: dict[str, Any]) -> dict[str, Any]:
        if hasattr(self.planner, "plan") and callable(getattr(self.planner, "plan")):
            response = self.planner.plan(payload)
        elif callable(self.planner):
            response = self.planner(payload)
        else:
            raise TypeError("planner must be callable or expose plan(payload)")
        if not isinstance(response, Mapping):
            raise TypeError(f"planner returned non-object payload: {type(response).__name__}")
        return dict(response)

    def _invoke_tool(self, *, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        descriptor = self.tool_registry.get(tool_name)
        if descriptor is None:
            raise KeyError(f"unknown tool: {tool_name}")

        tool_fn = _resolve_tool_callable(descriptor.get("callable"))
        result = tool_fn(payload)
        if not isinstance(result, dict):
            raise TypeError(f"tool {tool_name!r} returned non-object payload: {type(result).__name__}")
        return result

    def _resolve_template_paths(self, param_space: Mapping[str, Any]) -> tuple[Path, Path]:
        params_rel = param_space.get("template_params_path")
        if not isinstance(params_rel, str) or not params_rel.strip():
            key_mapping = param_space.get("key_mapping")
            if isinstance(key_mapping, Mapping):
                fallback_params = key_mapping.get("params_template")
                if isinstance(fallback_params, str) and fallback_params.strip():
                    params_rel = fallback_params
        if not isinstance(params_rel, str) or not params_rel.strip():
            raise ValueError("param_space is missing template_params_path")

        schedule_rel = param_space.get("template_schedule_path")
        if not isinstance(schedule_rel, str) or not schedule_rel.strip():
            global_safety = param_space.get("global_safety")
            if isinstance(global_safety, Mapping):
                fixed_params = global_safety.get("fixed_params")
                if isinstance(fixed_params, Mapping):
                    fallback_schedule = fixed_params.get("scale_outputs_file")
                    if isinstance(fallback_schedule, str) and fallback_schedule.strip():
                        schedule_rel = fallback_schedule
        if not isinstance(schedule_rel, str) or not schedule_rel.strip():
            raise ValueError("param_space is missing template schedule path")

        params_path = self._resolve_path(Path(params_rel.strip()))
        schedule_path = self._resolve_path(Path(schedule_rel.strip()))
        if not params_path.exists() or not params_path.is_file():
            raise FileNotFoundError(f"template params file not found: {params_path}")
        if not schedule_path.exists() or not schedule_path.is_file():
            raise FileNotFoundError(f"template schedule file not found: {schedule_path}")
        return (params_path, schedule_path)

    def _extract_proposed_params(self, plan_spec: PlanSpecV0) -> dict[str, Any]:
        metadata = plan_spec.metadata if isinstance(plan_spec.metadata, dict) else {}
        proposed = metadata.get("proposed_params")
        if isinstance(proposed, Mapping) and proposed:
            return dict(proposed)

        for call in plan_spec.tool_calls:
            if call.tool != "validate_params":
                continue
            params = call.payload.get("params")
            if isinstance(params, Mapping) and params:
                return dict(params)
        raise ValueError("planner output missing proposed_params in metadata")

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

        validate_payload = {"params": dict(proposed_params)}
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

        params_text = _render_params_text(template_params_text, proposed_params)
        rendered_params_path = artifacts_dir / "rendered_params.txt"
        rendered_schedule_path = artifacts_dir / "rendered_schedule.txt"
        rendered_params_path.write_text(params_text, encoding="utf-8")
        rendered_schedule_path.write_text(schedule_text, encoding="utf-8")
        artifact_paths["rendered_params_path"] = str(rendered_params_path)
        artifact_paths["rendered_schedule_path"] = str(rendered_schedule_path)

        run_payload = {
            "params_text": params_text,
            "schedule_text": schedule_text,
            "out_root": str((iter_dir / "backend_runs").resolve()),
            "run_id": f"{self.controller_run_id}_iter_{iteration:04d}",
        }
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

        compute_payload = {"run_manifest_path": run_manifest_path}
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

    def _invoke_and_record_tool_call(
        self,
        *,
        call_index: int,
        tool_name: str,
        payload: dict[str, Any],
        artifacts_dir: Path,
        start_time: float,
        tool_calls_used: int,
    ) -> tuple[dict[str, Any], str, int, str]:
        budget_reason = self._budget_termination(tool_calls_used=tool_calls_used, start_time=start_time)
        if budget_reason:
            return ({}, "", tool_calls_used, budget_reason)

        tool_calls_used += 1
        tool_result: dict[str, Any]
        try:
            tool_result = self._invoke_tool(tool_name=tool_name, payload=payload)
        except Exception as exc:  # noqa: BLE001
            tool_result = {"status": "error", "error": str(exc)}

        status = self._tool_call_status(tool_name=tool_name, result=tool_result)
        call_record: dict[str, Any] = {
            "index": call_index,
            "timestamp_utc": utc_now(),
            "tool": tool_name,
            "status": status,
            "payload": dict(payload),
            "result": dict(tool_result),
        }
        call_path = artifacts_dir / f"tool_{call_index:02d}_{tool_name}.json"
        _write_json(call_path, call_record)
        return (call_record, str(call_path), tool_calls_used, "")

    def _tool_call_status(self, *, tool_name: str, result: Mapping[str, Any]) -> str:
        if tool_name == "validate_params":
            return "success" if result.get("valid") is True else "failed"
        if tool_name == "run_cholla":
            return "success" if result.get("status") == "success" else "failed"
        if tool_name == "compute_metric":
            details = result.get("details")
            details_map = dict(details) if isinstance(details, Mapping) else {}
            return "success" if details_map.get("status") == "success" else "failed"

        status = result.get("status")
        if isinstance(status, str) and status.lower() in {"success", "ok", "valid"}:
            return "success"
        if isinstance(status, str):
            return "failed"
        return "success"

    def _planner_seed(self) -> int:
        raw_seed = getattr(self.planner, "seed", 0)
        if isinstance(raw_seed, int) and not isinstance(raw_seed, bool):
            return raw_seed
        return 0

    def _linked_experiment_id(self, state: Mapping[str, Any]) -> str:
        for key in ("linked_experiment_id", "run_experiment_id", "upstream_experiment_id"):
            value = state.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _run_id_from_tool_results(self, tool_results: list[dict[str, Any]]) -> str | None:
        for call in tool_results:
            if call.get("tool") != "run_cholla":
                continue
            result = call.get("result")
            result_map = dict(result) if isinstance(result, Mapping) else {}
            run_id = result_map.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
        return None

    def _controller_bundle_dir(self, experiment_id: str) -> Path:
        return self.repo_root / "agent" / "experiments" / experiment_id / "controller"

    def _write_controller_bundle(
        self,
        *,
        experiment_id: str,
        run_dir: Path,
        param_space: Mapping[str, Any],
        template_params_path: Path,
        template_schedule_path: Path,
        history_records: list[dict[str, Any]],
        seed: int,
        termination_reason: str,
        linked_experiment_id: str,
    ) -> Path:
        bundle_dir = self._controller_bundle_dir(experiment_id)
        bundle_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "experiment_id": experiment_id,
            "controller_run_id": self.controller_run_id,
            "seed": seed,
            "max_iterations": self.max_iterations,
            "max_tool_calls": self.max_tool_calls,
            "walltime_budget_sec": self.walltime_budget_sec,
            "param_space_source_path": str(self.param_space_path),
            "template_params_path": str(template_params_path),
            "template_schedule_path": str(template_schedule_path),
            "history_path": str(self.history_writer.path),
            "run_dir": str(run_dir),
            "linked_experiment_id": linked_experiment_id or None,
            "agent_types": {
                "summarizer": f"{type(self.summarizer).__module__}.{type(self.summarizer).__name__}",
                "planner": f"{type(self.planner).__module__}.{type(self.planner).__name__}",
            },
        }
        _write_json(bundle_dir / "config.json", config)
        (bundle_dir / "history_controller.jsonl").write_text(_jsonl_text(history_records), encoding="utf-8")
        (bundle_dir / "param_space.yaml").write_text(
            yaml.safe_dump(dict(param_space), sort_keys=True),
            encoding="utf-8",
        )

        iteration_records = [
            record
            for record in history_records
            if record.get("record_type") == "iteration" and isinstance(record.get("iteration"), int)
        ]
        iter_root = bundle_dir / "iterations"
        iter_root.mkdir(parents=True, exist_ok=True)
        for record in iteration_records:
            iteration = int(record["iteration"])
            snapshot = {
                "iteration": iteration,
                "status": record.get("status"),
                "RUN_ID": record.get("RUN_ID"),
                "summary": record.get("summary"),
                "plan": record.get("plan"),
                "tool_results": record.get("tool_results"),
                "termination_reason": record.get("termination_reason"),
                "error": record.get("error"),
            }
            _write_json(iter_root / f"iter_{iteration}.json", snapshot)

        summary_payload = {
            "experiment_id": experiment_id,
            "controller_run_id": self.controller_run_id,
            "seed": seed,
            "iterations_total": len(iteration_records),
            "termination_reason": termination_reason,
            "run_dir": str(run_dir),
            "bundle_files": {
                "config": "config.json",
                "history": "history_controller.jsonl",
                "param_space": "param_space.yaml",
                "iterations": "iterations/",
                "summary": "summary.json",
            },
        }
        _write_json(bundle_dir / "summary.json", summary_payload)
        return bundle_dir

    def replay(self, experiment_id: str) -> dict[str, Any]:
        """Replay controller advisory outputs and verify stored integrity."""

        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValueError("experiment_id must be a non-empty string")
        experiment_id = experiment_id.strip()

        bundle_dir = self._controller_bundle_dir(experiment_id)
        config_path = bundle_dir / "config.json"
        param_space_path = bundle_dir / "param_space.yaml"
        history_path = bundle_dir / "history_controller.jsonl"

        if not config_path.exists():
            raise FileNotFoundError(f"missing controller replay config: {config_path}")
        if not param_space_path.exists():
            raise FileNotFoundError(f"missing controller replay spec: {param_space_path}")
        if not history_path.exists():
            raise FileNotFoundError(f"missing controller replay history: {history_path}")

        config = _load_json(config_path)
        controller_run_id = config.get("controller_run_id")
        if not isinstance(controller_run_id, str) or not controller_run_id:
            raise ValueError(f"invalid controller_run_id in {config_path}")

        param_space_payload = yaml.safe_load(param_space_path.read_text(encoding="utf-8"))
        if not isinstance(param_space_payload, dict):
            raise ValueError(f"invalid param_space payload in {param_space_path}")

        all_records = _load_jsonl(history_path)
        iteration_records = [
            record
            for record in all_records
            if record.get("record_type") == "iteration" and isinstance(record.get("iteration"), int)
        ]
        iteration_records.sort(key=lambda entry: int(entry["iteration"]))

        replay_state: dict[str, Any] = {
            "param_space": param_space_payload,
            "history_path": str(history_path),
            "history_records": [],
        }

        params_checked = 0
        run_ids_checked = 0
        for record in iteration_records:
            iteration = int(record["iteration"])
            budgets = record.get("budgets")
            budgets_map = dict(budgets) if isinstance(budgets, Mapping) else {}

            summary_input = {
                "iteration": iteration,
                "state": replay_state,
                "budgets": budgets_map,
                "param_space": param_space_payload,
            }
            expected_summary = validate_summary_spec(self._invoke_summarizer(summary_input)).to_dict()
            observed_summary = record.get("summary")
            if not isinstance(observed_summary, Mapping):
                raise ValueError(f"replay mismatch iteration {iteration}: missing stored summary")
            summary_diff = _first_diff(expected_summary, dict(observed_summary), path="summary")
            if summary_diff:
                raise ValueError(f"replay mismatch iteration {iteration}: {summary_diff}")

            state_patch = expected_summary.get("state_patch")
            if isinstance(state_patch, Mapping):
                replay_state.update(dict(state_patch))

            planner_input = {
                "iteration": iteration,
                "state": replay_state,
                "budgets": budgets_map,
                "summary": expected_summary,
                "param_space": param_space_payload,
                "history_path": str(history_path),
            }
            expected_plan = validate_plan_spec(self._invoke_planner(planner_input)).to_dict()
            observed_plan = record.get("plan")
            if not isinstance(observed_plan, Mapping):
                raise ValueError(f"replay mismatch iteration {iteration}: missing stored plan")
            plan_diff = _first_diff(expected_plan, dict(observed_plan), path="plan")
            if plan_diff:
                raise ValueError(f"replay mismatch iteration {iteration}: {plan_diff}")

            expected_params = self._extract_proposed_params(validate_plan_spec(expected_plan))
            observed_params = self._extract_proposed_params(validate_plan_spec(dict(observed_plan)))
            params_diff = _first_diff(expected_params, observed_params, path="proposed_params")
            if params_diff:
                raise ValueError(f"replay mismatch iteration {iteration}: {params_diff}")
            params_checked += 1

            expected_run_id = f"{controller_run_id}_iter_{iteration:04d}"
            history_run_id = record.get("RUN_ID")
            if history_run_id != expected_run_id:
                raise ValueError(
                    "replay mismatch iteration "
                    f"{iteration}: RUN_ID history mismatch expected {expected_run_id!r}, got {history_run_id!r}"
                )

            tool_results_raw = record.get("tool_results")
            tool_results = tool_results_raw if isinstance(tool_results_raw, list) else []
            tool_run_id = self._run_id_from_tool_results([dict(item) for item in tool_results if isinstance(item, Mapping)])
            if tool_run_id != history_run_id:
                raise ValueError(
                    "replay mismatch iteration "
                    f"{iteration}: RUN_ID tool mismatch history={history_run_id!r} tool={tool_run_id!r}"
                )
            run_ids_checked += 1

            replay_state["last_iteration"] = iteration
            replay_state["last_summary"] = expected_summary
            replay_state["last_plan"] = expected_plan
            replay_state["last_tool_results"] = tool_results
            history_records_state = replay_state.get("history_records")
            if isinstance(history_records_state, list):
                history_records_state.append(record)

        replay_check_result: dict[str, Any] | None = None
        linked_experiment_id = config.get("linked_experiment_id")
        if isinstance(linked_experiment_id, str) and linked_experiment_id:
            if "replay_check" in self.tool_registry:
                replay_check_result = self._invoke_tool(
                    tool_name="replay_check",
                    payload={"experiment_id": linked_experiment_id},
                )
                replay_status = replay_check_result.get("status")
                if replay_status != "ok":
                    replay_error = replay_check_result.get("error")
                    raise ValueError(
                        "replay_check mismatch for linked experiment "
                        f"{linked_experiment_id!r}: {replay_error}"
                    )

        return {
            "status": "ok",
            "experiment_id": experiment_id,
            "iterations_checked": len(iteration_records),
            "params_checked": params_checked,
            "run_ids_checked": run_ids_checked,
            "replay_check": replay_check_result,
        }

    def _budget_termination(self, *, tool_calls_used: int, start_time: float) -> str:
        if tool_calls_used >= self.max_tool_calls:
            return "max_tool_calls_reached"
        if self.walltime_budget_sec is not None and self._elapsed_sec(start_time) >= self.walltime_budget_sec:
            return "walltime_budget_exceeded"
        return ""

    def _budget_snapshot(self, *, iterations_completed: int, tool_calls_used: int, start_time: float) -> dict[str, Any]:
        elapsed = self._elapsed_sec(start_time)
        remaining_walltime = (
            max(self.walltime_budget_sec - elapsed, 0.0) if self.walltime_budget_sec is not None else None
        )
        return {
            "max_iterations": self.max_iterations,
            "max_tool_calls": self.max_tool_calls,
            "walltime_budget_sec": self.walltime_budget_sec,
            "iterations_completed": iterations_completed,
            "tool_calls_used": tool_calls_used,
            "elapsed_sec": elapsed,
            "remaining_iterations": max(self.max_iterations - iterations_completed, 0),
            "remaining_tool_calls": max(self.max_tool_calls - tool_calls_used, 0),
            "remaining_walltime_sec": remaining_walltime,
        }

    def _elapsed_sec(self, start_time: float) -> float:
        return max(0.0, float(time.monotonic() - start_time))


__all__ = ["HybridController"]
