"""Hybrid controller scaffold for advisory planning over Tier-4 tools."""

from __future__ import annotations

import importlib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Mapping

import yaml

from agent.controller.history import HistoryRecordV0, HistoryWriter, utc_now
from agent.controller.specs import (
    PlanSpecV0,
    SummarySpecV0,
    validate_plan_spec,
    validate_planner_input,
    validate_planner_output,
    validate_summarizer_input,
    validate_summarizer_output,
)
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


def _text_for_artifact(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, indent=2, default=str)


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


def _agent_attr(agent_obj: Any, attr_name: str) -> Any:
    value = getattr(agent_obj, attr_name, None)
    if value is not None:
        return value
    base_obj = getattr(agent_obj, "base", None)
    return getattr(base_obj, attr_name, None)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _optional_non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _agent_max_attempts(agent_obj: Any) -> int | None:
    for attr_name in ("max_attempts", "stability_attempts"):
        value = _optional_int(_agent_attr(agent_obj, attr_name))
        if value is not None:
            return value
    return None


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
        max_failures: int = 1,
        require_live_lm: bool = True,
        tool_backend: str = "real",
        bundle_metadata: Mapping[str, Any] | None = None,
        effective_config: Mapping[str, Any] | None = None,
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
        self.max_failures = int(max_failures)
        self.require_live_lm = bool(require_live_lm)
        self.tool_backend = tool_backend.strip().lower() if isinstance(tool_backend, str) else ""
        if self.tool_backend not in {"real", "mock"}:
            raise ValueError(f"tool_backend must be one of ['real', 'mock'], got {tool_backend!r}")
        self.bundle_metadata = dict(bundle_metadata) if isinstance(bundle_metadata, Mapping) else {}
        self.effective_config = dict(effective_config) if isinstance(effective_config, Mapping) else None

        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be >= 0")
        if self.max_failures < 1:
            raise ValueError("max_failures must be >= 1")
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
        tasks_dir = self._controller_tasks_dir(self.experiment_id)
        tasks_dir.mkdir(parents=True, exist_ok=True)

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

            summary_input_payload = {
                "iteration": iteration,
                "state": state,
                "budgets": budgets,
                "param_space": param_space,
            }
            summary_input_path = artifacts_dir / "summary_input.json"
            summary_task_status = "success"
            summary_task_error: str | None = None
            summary_task_outputs: list[Path] = []
            summary_task_inputs: list[Path] = []

            try:
                summary_input = validate_summarizer_input(summary_input_payload)
                _write_json(summary_input_path, summary_input)
                artifact_paths["summary_input_path"] = str(summary_input_path)
                summary_task_inputs.append(summary_input_path)
                summary_spec = validate_summarizer_output(self._invoke_summarizer(summary_input))
                summary_path = artifacts_dir / "summary_spec.json"
                _write_json(summary_path, summary_spec.to_dict())
                artifact_paths["summary_spec_path"] = str(summary_path)
                summary_task_outputs.append(summary_path)
                if summary_spec.state_patch:
                    state.update(summary_spec.state_patch)
            except Exception as exc:  # noqa: BLE001
                summary_task_status = "failed"
                summary_task_error = str(exc)
                iteration_status = "failed"
                iteration_error = f"summarizer_error: {exc}"
                iteration_termination = "summarizer_error"
            finally:
                try:
                    summarizer_task_path = self._write_agent_task_envelope(
                        tasks_dir=tasks_dir,
                        iteration=iteration,
                        agent_kind="summarizer",
                        input_payload=summary_input_payload,
                        input_artifacts=summary_task_inputs,
                        output_artifacts=summary_task_outputs,
                        status=summary_task_status,
                        error=summary_task_error,
                    )
                    artifact_paths["summarizer_task_path"] = str(summarizer_task_path)
                except Exception as task_exc:  # noqa: BLE001
                    task_error = f"summarizer_task_error: {task_exc}"
                    iteration_status = "failed"
                    iteration_error = f"{iteration_error}; {task_error}" if iteration_error else task_error
                    iteration_termination = "summarizer_task_error"

            if iteration_status == "success" and summary_spec is not None and summary_spec.should_stop:
                iteration_status = "terminated"
                iteration_termination = summary_spec.termination_reason_hint or "summarizer_requested_stop"

            if iteration_status == "success":
                planner_input_payload = {
                    "iteration": iteration,
                    "state": state,
                    "budgets": budgets,
                    "summary": summary_spec.to_dict() if summary_spec is not None else {},
                    "param_space": param_space,
                    "history_path": str(self.history_writer.path),
                }
                planner_input_path = artifacts_dir / "planner_input.json"
                planner_task_status = "success"
                planner_task_error: str | None = None
                planner_task_outputs: list[Path] = []
                planner_task_inputs: list[Path] = []

                try:
                    planner_input = validate_planner_input(planner_input_payload)
                    _write_json(planner_input_path, planner_input)
                    artifact_paths["planner_input_path"] = str(planner_input_path)
                    planner_task_inputs.append(planner_input_path)
                    plan_spec = validate_planner_output(self._invoke_planner(planner_input))
                    plan_path = artifacts_dir / "plan_spec.json"
                    _write_json(plan_path, plan_spec.to_dict())
                    artifact_paths["plan_spec_path"] = str(plan_path)
                    planner_task_outputs.append(plan_path)
                except Exception as exc:  # noqa: BLE001
                    planner_task_status = "failed"
                    planner_task_error = str(exc)
                    iteration_status = "failed"
                    iteration_error = f"planner_error: {exc}"
                    iteration_termination = "planner_error"
                finally:
                    try:
                        planner_task_path = self._write_agent_task_envelope(
                            tasks_dir=tasks_dir,
                            iteration=iteration,
                            agent_kind="planner",
                            input_payload=planner_input_payload,
                            input_artifacts=planner_task_inputs,
                            output_artifacts=planner_task_outputs,
                            status=planner_task_status,
                            error=planner_task_error,
                        )
                        artifact_paths["planner_task_path"] = str(planner_task_path)
                    except Exception as task_exc:  # noqa: BLE001
                        task_error = f"planner_task_error: {task_exc}"
                        iteration_status = "failed"
                        iteration_error = f"{iteration_error}; {task_error}" if iteration_error else task_error
                        iteration_termination = "planner_task_error"

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

            if iteration_status == "failed":
                if failed_iterations >= self.max_failures:
                    termination_reason = "max_failures_reached"
                    break
                continue

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
            "successful_iterations": max(iterations_completed - failed_iterations, 0),
            "failed_iterations": failed_iterations,
            "max_failures": self.max_failures,
            "require_live_lm": self.require_live_lm,
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
        self._enforce_live_lm(agent_kind="summarizer")
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
        self._enforce_live_lm(agent_kind="planner")
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

    def _tool_call_by_name(self, tool_results: list[dict[str, Any]], tool_name: str) -> dict[str, Any] | None:
        for call in tool_results:
            if call.get("tool") == tool_name:
                return dict(call)
        return None

    def _metric_scalar_from_tool_results(self, tool_results: list[dict[str, Any]]) -> float | None:
        for call in tool_results:
            if call.get("tool") != "compute_metric":
                continue
            result = call.get("result")
            result_map = dict(result) if isinstance(result, Mapping) else {}
            scalar = result_map.get("scalar")
            if isinstance(scalar, (int, float)) and not isinstance(scalar, bool):
                return float(scalar)
            metric_value = result_map.get("metric_value")
            if isinstance(metric_value, (int, float)) and not isinstance(metric_value, bool):
                return float(metric_value)
        return None

    def _replay_invoke_tool_call(
        self,
        *,
        call_index: int,
        tool_name: str,
        payload: dict[str, Any],
        artifacts_dir: Path,
    ) -> dict[str, Any]:
        try:
            result = self._invoke_tool(tool_name=tool_name, payload=payload)
        except Exception as exc:  # noqa: BLE001
            result = {"status": "error", "error": str(exc)}

        status = self._tool_call_status(tool_name=tool_name, result=result)
        call_record = {
            "index": call_index,
            "timestamp_utc": utc_now(),
            "tool": tool_name,
            "status": status,
            "payload": dict(payload),
            "result": dict(result),
        }
        _write_json(artifacts_dir / f"replay_tool_{call_index:02d}_{tool_name}.json", call_record)
        return call_record

    def _replay_execute_iteration_tool_chain(
        self,
        *,
        iteration: int,
        controller_run_id: str,
        proposed_params: dict[str, Any],
        template_params_text: str,
        schedule_text: str,
        replay_iter_dir: Path,
        artifacts_dir: Path,
    ) -> list[dict[str, Any]]:
        replay_iter_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        tool_results: list[dict[str, Any]] = []

        validate_payload = {"params": dict(proposed_params)}
        validate_call = self._replay_invoke_tool_call(
            call_index=0,
            tool_name="validate_params",
            payload=validate_payload,
            artifacts_dir=artifacts_dir,
        )
        tool_results.append(validate_call)
        validate_result = validate_call.get("result")
        validate_result_map = dict(validate_result) if isinstance(validate_result, Mapping) else {}
        if validate_call.get("status") != "success" or validate_result_map.get("valid") is not True:
            errors = validate_result_map.get("errors")
            raise ValueError(
                f"replay mismatch iteration {iteration}: validate_params replay failed with errors={errors!r}"
            )

        params_text = _render_params_text(template_params_text, proposed_params)
        rendered_params_path = artifacts_dir / "replay_rendered_params.txt"
        rendered_schedule_path = artifacts_dir / "replay_rendered_schedule.txt"
        rendered_params_path.write_text(params_text, encoding="utf-8")
        rendered_schedule_path.write_text(schedule_text, encoding="utf-8")

        expected_run_id = f"{controller_run_id}_iter_{iteration:04d}"
        run_payload = {
            "params_text": params_text,
            "schedule_text": schedule_text,
            "out_root": str((replay_iter_dir / "backend_runs").resolve()),
            "run_id": expected_run_id,
        }
        run_call = self._replay_invoke_tool_call(
            call_index=1,
            tool_name="run_cholla",
            payload=run_payload,
            artifacts_dir=artifacts_dir,
        )
        tool_results.append(run_call)
        run_result = run_call.get("result")
        run_result_map = dict(run_result) if isinstance(run_result, Mapping) else {}
        if run_call.get("status") != "success":
            raise ValueError(
                "replay mismatch iteration "
                f"{iteration}: run_cholla replay failed with error={run_result_map.get('error')!r}"
            )
        run_manifest_path = run_result_map.get("run_manifest_path")
        if not isinstance(run_manifest_path, str) or not run_manifest_path.strip():
            raise ValueError(f"replay mismatch iteration {iteration}: missing run_manifest_path from run_cholla")

        metric_call = self._replay_invoke_tool_call(
            call_index=2,
            tool_name="compute_metric",
            payload={"run_manifest_path": run_manifest_path},
            artifacts_dir=artifacts_dir,
        )
        tool_results.append(metric_call)
        metric_result = metric_call.get("result")
        metric_result_map = dict(metric_result) if isinstance(metric_result, Mapping) else {}
        metric_details = metric_result_map.get("details")
        metric_details_map = dict(metric_details) if isinstance(metric_details, Mapping) else {}
        if metric_call.get("status") != "success" or metric_details_map.get("status") != "success":
            raise ValueError(
                "replay mismatch iteration "
                f"{iteration}: compute_metric replay failed with error={metric_details_map.get('error')!r}"
            )

        return tool_results

    def _experiment_bundle_dir(self, experiment_id: str) -> Path:
        return self.repo_root / "agent" / "experiments" / experiment_id

    def _controller_bundle_dir(self, experiment_id: str) -> Path:
        return self._experiment_bundle_dir(experiment_id) / "controller"

    def _controller_tasks_dir(self, experiment_id: str) -> Path:
        return self._experiment_bundle_dir(experiment_id) / "tasks"

    def _agent_obj(self, agent_kind: str) -> Any:
        if agent_kind == "planner":
            return self.planner
        if agent_kind == "summarizer":
            return self.summarizer
        raise ValueError(f"unknown agent_kind: {agent_kind!r}")

    def _agent_lm_trace(self, agent_kind: str) -> dict[str, Any]:
        agent_obj = self._agent_obj(agent_kind)

        trace_raw = getattr(agent_obj, "last_lm_trace", None)
        if isinstance(trace_raw, Mapping):
            return dict(trace_raw)

        base_obj = getattr(agent_obj, "base", None)
        base_trace_raw = getattr(base_obj, "last_lm_trace", None)
        if isinstance(base_trace_raw, Mapping):
            return dict(base_trace_raw)
        return {}

    def _enforce_live_lm(self, *, agent_kind: str) -> None:
        if not self.require_live_lm:
            return

        trace = self._agent_lm_trace(agent_kind)
        if not trace:
            raise RuntimeError(f"{agent_kind}_live_lm_required: missing last_lm_trace")

        if trace.get("live_lm_used") is not True:
            error = trace.get("error")
            raise RuntimeError(f"{agent_kind}_live_lm_required: live LM not used ({error})")

        if str(trace.get("status", "")).lower() != "ok":
            error = trace.get("error")
            raise RuntimeError(f"{agent_kind}_live_lm_required: LM status not ok ({error})")

    def _agent_card(self, agent_kind: str) -> dict[str, Any]:
        if not hasattr(self, "_agent_cards_cache"):
            self._agent_cards_cache: dict[str, dict[str, Any]] = {}

        cached = self._agent_cards_cache.get(agent_kind)
        if cached is not None:
            return dict(cached)

        card_path = self.repo_root / "agent" / "agents" / "cards" / f"{agent_kind}.agent.json"
        if not card_path.exists() or not card_path.is_file():
            raise FileNotFoundError(f"missing agent card for {agent_kind!r}: {card_path}")

        card_payload = _load_json(card_path)
        required_fields = ("name", "version", "capabilities", "input_schema", "output_schema")
        missing_fields = [field for field in required_fields if field not in card_payload]
        if missing_fields:
            raise ValueError(f"agent card {card_path} missing required fields: {missing_fields}")

        self._agent_cards_cache[agent_kind] = dict(card_payload)
        return dict(card_payload)

    def _agent_runtime_audit(
        self,
        *,
        agent_kind: str,
        input_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        agent_obj = self._agent_obj(agent_kind)

        model_raw = getattr(agent_obj, "model", None)
        model = model_raw.strip() if isinstance(model_raw, str) and model_raw.strip() else None

        temp_raw = getattr(agent_obj, "temperature", None)
        if isinstance(temp_raw, (int, float)) and not isinstance(temp_raw, bool):
            temperature: float | None = float(temp_raw)
        else:
            temperature = None

        prompt_raw = getattr(agent_obj, "system_prompt", "")
        system_prompt = prompt_raw if isinstance(prompt_raw, str) else str(prompt_raw)
        hash_payload = {
            "system_prompt": system_prompt,
            "input": dict(input_payload),
        }
        prompt_input_hash = hashlib.sha256(
            json.dumps(hash_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

        lm_trace = self._agent_lm_trace(agent_kind)
        raw_outputs = lm_trace.get("raw_outputs")
        raw_outputs_list = list(raw_outputs) if isinstance(raw_outputs, list) else []
        attempts_raw = lm_trace.get("attempt_summaries")
        attempt_summaries = [dict(entry) for entry in attempts_raw] if isinstance(attempts_raw, list) else []
        accepted_attempt_index_raw = lm_trace.get("accepted_attempt_index")
        accepted_attempt_index = (
            int(accepted_attempt_index_raw)
            if isinstance(accepted_attempt_index_raw, int) and not isinstance(accepted_attempt_index_raw, bool)
            else None
        )
        max_attempts_raw = lm_trace.get("max_attempts")
        max_attempts = (
            int(max_attempts_raw)
            if isinstance(max_attempts_raw, int) and not isinstance(max_attempts_raw, bool)
            else None
        )
        if max_attempts is None:
            fallback_attempts_raw = getattr(agent_obj, "max_attempts", None)
            if not (
                isinstance(fallback_attempts_raw, int)
                and not isinstance(fallback_attempts_raw, bool)
            ):
                fallback_attempts_raw = getattr(agent_obj, "stability_attempts", None)
            if not (
                isinstance(fallback_attempts_raw, int)
                and not isinstance(fallback_attempts_raw, bool)
            ):
                base_obj = getattr(agent_obj, "base", None)
                fallback_attempts_raw = getattr(base_obj, "max_attempts", None)
                if not (
                    isinstance(fallback_attempts_raw, int)
                    and not isinstance(fallback_attempts_raw, bool)
                ):
                    fallback_attempts_raw = getattr(base_obj, "stability_attempts", None)
            if isinstance(fallback_attempts_raw, int) and not isinstance(fallback_attempts_raw, bool):
                max_attempts = int(fallback_attempts_raw)
        trace_seed_raw = lm_trace.get("seed")
        trace_seed = (
            int(trace_seed_raw)
            if isinstance(trace_seed_raw, int) and not isinstance(trace_seed_raw, bool)
            else None
        )
        if trace_seed is None:
            seed_raw = getattr(agent_obj, "seed", None)
            if not (isinstance(seed_raw, int) and not isinstance(seed_raw, bool)):
                base_obj = getattr(agent_obj, "base", None)
                seed_raw = getattr(base_obj, "seed", None)
            if isinstance(seed_raw, int) and not isinstance(seed_raw, bool):
                trace_seed = int(seed_raw)

        return {
            "model": model,
            "temperature": temperature,
            "seed": trace_seed,
            "prompt_input_hash": prompt_input_hash,
            "provider": lm_trace.get("provider"),
            "base_url": lm_trace.get("base_url"),
            "api_version": lm_trace.get("api_version"),
            "live_lm_used": lm_trace.get("live_lm_used"),
            "lm_status": lm_trace.get("status"),
            "lm_error": lm_trace.get("error"),
            "raw_outputs": raw_outputs_list,
            "attempt_summaries": attempt_summaries,
            "accepted_attempt_index": accepted_attempt_index,
            "max_attempts": max_attempts,
        }

    def _write_agent_task_envelope(
        self,
        *,
        tasks_dir: Path,
        iteration: int,
        agent_kind: str,
        input_payload: Mapping[str, Any],
        input_artifacts: list[Path],
        output_artifacts: list[Path],
        status: str,
        error: str | None,
    ) -> Path:
        tasks_dir.mkdir(parents=True, exist_ok=True)

        input_paths: list[str] = []
        for path in input_artifacts:
            if path.exists():
                input_paths.append(str(path))

        output_paths: list[str] = []
        for path in output_artifacts:
            if path.exists():
                output_paths.append(str(path))

        card = self._agent_card(agent_kind)
        audit = self._agent_runtime_audit(agent_kind=agent_kind, input_payload=input_payload)
        task_name = f"task_{iteration}_{agent_kind}"
        lm_raw_output_artifacts: list[str] = []
        attempts: list[dict[str, Any]] = []
        raw_outputs = audit.get("raw_outputs")
        raw_output_artifact_map: dict[int, str] = {}
        raw_dir = tasks_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(raw_outputs, list):
            for idx, raw_output in enumerate(raw_outputs):
                raw_path = raw_dir / f"{task_name}_attempt_{idx}.txt"
                raw_path.write_text(_text_for_artifact(raw_output), encoding="utf-8")
                lm_raw_output_artifacts.append(str(raw_path))
                raw_output_artifact_map[idx] = str(raw_path)
                output_paths.append(str(raw_path))

        attempt_summaries = audit.get("attempt_summaries")
        if isinstance(attempt_summaries, list):
            for idx, summary in enumerate(attempt_summaries):
                summary_map = dict(summary) if isinstance(summary, Mapping) else {}
                attempt_index_raw = summary_map.get("attempt_index")
                attempt_index = (
                    int(attempt_index_raw)
                    if isinstance(attempt_index_raw, int) and not isinstance(attempt_index_raw, bool)
                    else idx
                )
                raw_output_index_raw = summary_map.get("raw_output_index")
                raw_output_index = (
                    int(raw_output_index_raw)
                    if isinstance(raw_output_index_raw, int) and not isinstance(raw_output_index_raw, bool)
                    else None
                )
                raw_artifact = (
                    raw_output_artifact_map.get(raw_output_index) if raw_output_index is not None else None
                )
                attempts.append(
                    {
                        "attempt_index": attempt_index,
                        "status": summary_map.get("status"),
                        "error": summary_map.get("error"),
                        "raw_output_artifact": raw_artifact,
                        "json_parse_ok": summary_map.get("json_parse_ok"),
                        "schema_valid": summary_map.get("schema_valid"),
                        "constraints_valid": summary_map.get("constraints_valid"),
                        "accepted": summary_map.get("accepted"),
                    }
                )
        elif lm_raw_output_artifacts:
            for idx, path in enumerate(lm_raw_output_artifacts):
                attempts.append(
                    {
                        "attempt_index": idx,
                        "status": "unknown",
                        "error": None,
                        "raw_output_artifact": path,
                        "json_parse_ok": None,
                        "schema_valid": None,
                        "constraints_valid": None,
                        "accepted": None,
                    }
                )

        envelope = {
            "task_id": f"{self.experiment_id}:{self.controller_run_id}:{task_name}",
            "experiment_id": self.experiment_id,
            "controller_run_id": self.controller_run_id,
            "iteration": iteration,
            "agent_kind": agent_kind,
            "timestamp_utc": utc_now(),
            "status": status,
            "error": error,
            "agent_card": card,
            "input_artifacts": input_paths,
            "output_artifacts": output_paths,
            "model": audit["model"],
            "temperature": audit["temperature"],
            "seed": audit["seed"],
            "max_attempts": audit["max_attempts"],
            "accepted_attempt_index": audit["accepted_attempt_index"],
            "attempts": attempts,
            "prompt_input_hash": audit["prompt_input_hash"],
            "provider": audit["provider"],
            "base_url": audit["base_url"],
            "api_version": audit["api_version"],
            "live_lm_used": audit["live_lm_used"],
            "lm_status": audit["lm_status"],
            "lm_error": audit["lm_error"],
            "lm_raw_output_artifacts": lm_raw_output_artifacts,
        }
        task_path = tasks_dir / f"{task_name}.json"
        _write_json(task_path, envelope)
        return task_path

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

        planner_model = _optional_non_empty_str(_agent_attr(self.planner, "model"))
        summarizer_model = _optional_non_empty_str(_agent_attr(self.summarizer, "model"))
        lm_model = planner_model or summarizer_model

        planner_temperature = _optional_float(_agent_attr(self.planner, "temperature"))
        summarizer_temperature = _optional_float(_agent_attr(self.summarizer, "temperature"))
        lm_temperature = planner_temperature if planner_temperature is not None else summarizer_temperature

        planner_attempts = _agent_max_attempts(self.planner)
        summarizer_attempts = _agent_max_attempts(self.summarizer)
        max_lm_attempts = planner_attempts if planner_attempts is not None else summarizer_attempts

        config = {
            "experiment_id": experiment_id,
            "controller_run_id": self.controller_run_id,
            "seed": seed,
            "max_iterations": self.max_iterations,
            "max_tool_calls": self.max_tool_calls,
            "max_failures": self.max_failures,
            "require_live_lm": self.require_live_lm,
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
            "tool_backend": self.tool_backend,
            "head_sha": _optional_non_empty_str(self.bundle_metadata.get("head_sha")),
            "HEAD_SHA": _optional_non_empty_str(self.bundle_metadata.get("head_sha")),
            "controller_version": _optional_non_empty_str(self.bundle_metadata.get("controller_version")),
            "lm_model": lm_model,
            "lm_temperature": lm_temperature,
            "max_lm_attempts": max_lm_attempts,
            "acceptance_replay_strict": bool(self.bundle_metadata.get("acceptance_replay_strict")),
            "cli_invocation": _optional_non_empty_str(self.bundle_metadata.get("cli_invocation")),
        }
        config_effective_path = bundle_dir / "config_effective.yaml"
        if self.effective_config is not None:
            config["config_effective_path"] = "config_effective.yaml"

        _write_json(bundle_dir / "config.json", config)
        (bundle_dir / "history_controller.jsonl").write_text(_jsonl_text(history_records), encoding="utf-8")
        (bundle_dir / "param_space.yaml").write_text(
            yaml.safe_dump(dict(param_space), sort_keys=True),
            encoding="utf-8",
        )
        if self.effective_config is not None:
            config_effective_path.write_text(
                yaml.safe_dump(dict(self.effective_config), sort_keys=True),
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
            "head_sha": _optional_non_empty_str(self.bundle_metadata.get("head_sha")),
            "HEAD_SHA": _optional_non_empty_str(self.bundle_metadata.get("head_sha")),
            "controller_version": _optional_non_empty_str(self.bundle_metadata.get("controller_version")),
            "lm_model": lm_model,
            "lm_temperature": lm_temperature,
            "max_lm_attempts": max_lm_attempts,
            "acceptance_replay_strict": bool(self.bundle_metadata.get("acceptance_replay_strict")),
            "cli_invocation": _optional_non_empty_str(self.bundle_metadata.get("cli_invocation")),
            "tool_backend": self.tool_backend,
            "bundle_files": {
                "config": "config.json",
                "history": "history_controller.jsonl",
                "param_space": "param_space.yaml",
                "iterations": "iterations/",
                "summary": "summary.json",
            },
        }
        if self.effective_config is not None:
            summary_payload["bundle_files"]["config_effective"] = "config_effective.yaml"
        _write_json(bundle_dir / "summary.json", summary_payload)
        return bundle_dir

    def replay(self, experiment_id: str, replay_mode: str = "strict") -> dict[str, Any]:
        """Replay controller records in strict or live mode.

        strict: reuse stored SummarySpec/PlanSpec and re-run tools; enforce params, RUN_ID, and metric scalar.
        live: strict checks plus fresh LM calls with non-fatal diffs against stored SummarySpec/PlanSpec.
        """

        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValueError("experiment_id must be a non-empty string")
        experiment_id = experiment_id.strip()

        replay_mode_norm = replay_mode.strip().lower() if isinstance(replay_mode, str) else ""
        if replay_mode_norm not in {"strict", "live"}:
            raise ValueError(f"replay_mode must be one of ['strict', 'live'], got {replay_mode!r}")

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

        template_params_path, template_schedule_path = self._resolve_template_paths(param_space_payload)
        template_params_text = template_params_path.read_text(encoding="utf-8")
        schedule_text = template_schedule_path.read_text(encoding="utf-8")

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
        replay_artifacts_root = bundle_dir / "replay" / replay_mode_norm
        replay_artifacts_root.mkdir(parents=True, exist_ok=True)

        params_checked = 0
        run_ids_checked = 0
        metrics_checked = 0
        live_diffs: list[dict[str, Any]] = []

        for record in iteration_records:
            iteration = int(record["iteration"])
            budgets = record.get("budgets")
            budgets_map = dict(budgets) if isinstance(budgets, Mapping) else {}

            observed_summary_raw = record.get("summary")
            if not isinstance(observed_summary_raw, Mapping):
                raise ValueError(f"replay mismatch iteration {iteration}: missing stored summary")
            observed_summary = validate_summarizer_output(dict(observed_summary_raw)).to_dict()

            observed_plan_raw = record.get("plan")
            if not isinstance(observed_plan_raw, Mapping):
                raise ValueError(f"replay mismatch iteration {iteration}: missing stored plan")
            observed_plan = validate_planner_output(dict(observed_plan_raw)).to_dict()
            observed_plan_spec = validate_plan_spec(observed_plan)

            if replay_mode_norm == "live":
                try:
                    live_summary_input = validate_summarizer_input(
                        {
                            "iteration": iteration,
                            "state": replay_state,
                            "budgets": budgets_map,
                            "param_space": param_space_payload,
                        }
                    )
                    live_summary = validate_summarizer_output(self._invoke_summarizer(live_summary_input)).to_dict()
                    summary_diff = _first_diff(live_summary, observed_summary, path="summary")
                    if summary_diff:
                        live_diffs.append(
                            {
                                "iteration": iteration,
                                "artifact": "summary",
                                "diff": summary_diff,
                            }
                        )

                    live_planner_input = validate_planner_input(
                        {
                            "iteration": iteration,
                            "state": replay_state,
                            "budgets": budgets_map,
                            "summary": live_summary,
                            "param_space": param_space_payload,
                            "history_path": str(history_path),
                        }
                    )
                    live_plan = validate_planner_output(self._invoke_planner(live_planner_input)).to_dict()
                    plan_diff = _first_diff(live_plan, observed_plan, path="plan")
                    if plan_diff:
                        live_diffs.append(
                            {
                                "iteration": iteration,
                                "artifact": "plan",
                                "diff": plan_diff,
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    live_diffs.append(
                        {
                            "iteration": iteration,
                            "artifact": "live_requery",
                            "diff": f"live_requery_error: {exc}",
                        }
                    )

            state_patch = observed_summary.get("state_patch")
            if isinstance(state_patch, Mapping):
                replay_state.update(dict(state_patch))

            tool_results_raw = record.get("tool_results")
            observed_tool_results = [dict(item) for item in tool_results_raw if isinstance(item, Mapping)] if isinstance(
                tool_results_raw, list
            ) else []

            if not observed_plan_spec.should_stop:
                expected_params = self._extract_proposed_params(observed_plan_spec)
                stored_validate_call = self._tool_call_by_name(observed_tool_results, "validate_params")
                stored_run_call = self._tool_call_by_name(observed_tool_results, "run_cholla")
                stored_metric_call = self._tool_call_by_name(observed_tool_results, "compute_metric")
                should_replay_tools = (
                    stored_validate_call is not None
                    and stored_run_call is not None
                    and stored_metric_call is not None
                    and stored_run_call.get("status") == "success"
                    and stored_metric_call.get("status") == "success"
                )
                if stored_validate_call is not None:
                    stored_validate_payload = (
                        dict(stored_validate_call.get("payload"))
                        if isinstance(stored_validate_call.get("payload"), Mapping)
                        else {}
                    )
                    stored_params_raw = stored_validate_payload.get("params")
                    stored_params = dict(stored_params_raw) if isinstance(stored_params_raw, Mapping) else {}
                    stored_params_diff = _first_diff(expected_params, stored_params, path="stored.params")
                    if stored_params_diff:
                        raise ValueError(f"replay mismatch iteration {iteration}: {stored_params_diff}")
                params_checked += 1

                if should_replay_tools:
                    replay_iter_dir = replay_artifacts_root / f"iter_{iteration}"
                    replay_tool_results = self._replay_execute_iteration_tool_chain(
                        iteration=iteration,
                        controller_run_id=controller_run_id,
                        proposed_params=expected_params,
                        template_params_text=template_params_text,
                        schedule_text=schedule_text,
                        replay_iter_dir=replay_iter_dir,
                        artifacts_dir=replay_iter_dir / "artifacts",
                    )

                    replay_validate_call = self._tool_call_by_name(replay_tool_results, "validate_params")
                    replay_validate_payload = (
                        dict(replay_validate_call.get("payload"))
                        if isinstance(replay_validate_call, Mapping) and isinstance(replay_validate_call.get("payload"), Mapping)
                        else {}
                    )
                    replay_params_raw = replay_validate_payload.get("params")
                    replay_params = dict(replay_params_raw) if isinstance(replay_params_raw, Mapping) else {}
                    replay_params_diff = _first_diff(expected_params, replay_params, path="replay.params")
                    if replay_params_diff:
                        raise ValueError(f"replay mismatch iteration {iteration}: {replay_params_diff}")

                    history_run_id = record.get("RUN_ID")
                    if not isinstance(history_run_id, str) or not history_run_id:
                        raise ValueError(
                            "replay mismatch iteration "
                            f"{iteration}: missing history RUN_ID"
                        )

                    replay_run_id = self._run_id_from_tool_results(replay_tool_results)
                    if not isinstance(replay_run_id, str) or not replay_run_id:
                        raise ValueError(
                            "replay mismatch iteration "
                            f"{iteration}: missing replay RUN_ID from run_cholla result"
                        )
                    if replay_run_id != history_run_id:
                        raise ValueError(
                            "replay mismatch iteration "
                            f"{iteration}: RUN_ID replay mismatch history={history_run_id!r} replay={replay_run_id!r}"
                        )

                    stored_tool_run_id = self._run_id_from_tool_results(observed_tool_results)
                    if not isinstance(stored_tool_run_id, str) or not stored_tool_run_id:
                        raise ValueError(
                            "replay mismatch iteration "
                            f"{iteration}: missing stored RUN_ID in run_cholla result"
                        )
                    if stored_tool_run_id != history_run_id:
                        raise ValueError(
                            "replay mismatch iteration "
                            f"{iteration}: RUN_ID stored tool mismatch history={history_run_id!r} tool={stored_tool_run_id!r}"
                        )
                    run_ids_checked += 1

                    observed_metric_scalar = self._metric_scalar_from_tool_results(observed_tool_results)
                    replay_metric_scalar = self._metric_scalar_from_tool_results(replay_tool_results)
                    metric_diff = _first_diff(observed_metric_scalar, replay_metric_scalar, path="metric.scalar")
                    if metric_diff:
                        raise ValueError(f"replay mismatch iteration {iteration}: {metric_diff}")
                    metrics_checked += 1

                    replay_state["last_tool_results"] = replay_tool_results
                else:
                    replay_state["last_tool_results"] = observed_tool_results
            else:
                replay_state["last_tool_results"] = observed_tool_results

            replay_state["last_iteration"] = iteration
            replay_state["last_summary"] = observed_summary
            replay_state["last_plan"] = observed_plan
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

        result: dict[str, Any] = {
            "status": "ok",
            "experiment_id": experiment_id,
            "replay_mode": replay_mode_norm,
            "iterations_checked": len(iteration_records),
            "params_checked": params_checked,
            "run_ids_checked": run_ids_checked,
            "metrics_checked": metrics_checked,
            "error": None,
            "replay_check": replay_check_result,
        }
        if replay_mode_norm == "live":
            result["live_diffs"] = live_diffs
        return result

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
