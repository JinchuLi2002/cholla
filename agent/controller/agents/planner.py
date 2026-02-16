"""Deterministic mock planner agent for Phase 3B advisory flow."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from agent.controller.specs import PlanSpecV0, SummarySpecV0, validate_plan_spec, validate_summary_spec


@dataclass(frozen=True)
class MockPlannerAgent:
    """Create deterministic PlanSpec payloads from summary + param-space input."""

    seed: int = 0
    init_redshift_step: float = 0.02

    def plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a PlanSpecV0 payload with deterministic next-params proposal."""

        if self.init_redshift_step <= 0:
            raise ValueError("init_redshift_step must be > 0")

        payload_map = dict(payload)
        iteration = _as_int(payload_map.get("iteration"), default=0)
        state = _coerce_mapping(payload_map.get("state"))
        param_space = _coerce_mapping(payload_map.get("param_space"))
        if not param_space:
            raise ValueError("planner payload missing param_space")

        summary = _coerce_summary(payload_map.get("summary"))
        params_spec = _parameter_specs(param_space)
        proposed_params = _default_params(params_spec)

        self._adjust_grid_side(proposed_params, params_spec, iteration=iteration)
        self._adjust_init_redshift(
            proposed_params,
            params_spec,
            summary=summary,
            iteration=iteration,
            state=state,
        )

        should_stop = summary.should_stop
        termination_reason = summary.termination_reason_hint if should_stop else None

        trend = str(summary.state_patch.get("trend", "insufficient_data"))
        best_scalar = summary.state_patch.get("best_scalar")
        rationale = (
            f"iter={iteration} trend={trend} "
            f"best_scalar={_format_scalar(best_scalar)} seed={self.seed}"
        )

        tool_calls: list[dict[str, Any]] = []
        if not should_stop:
            tool_calls.append(
                {
                    "tool": "validate_params",
                    "payload": {"params": proposed_params},
                    "note": "deterministic preflight bounds check",
                }
            )

        raw_plan = {
            "version": "v0",
            "should_stop": should_stop,
            "termination_reason": termination_reason,
            "rationale": rationale,
            "metadata": {
                "seed": self.seed,
                "iteration": iteration,
                "summary_trend": trend,
                "summary_best_scalar": best_scalar,
                "proposed_params": proposed_params,
            },
            "tool_calls": tool_calls,
        }
        validated: PlanSpecV0 = validate_plan_spec(raw_plan)
        return validated.to_dict()

    def plan_json(self, payload: Mapping[str, Any]) -> str:
        """Return canonical JSON output for deterministic comparisons."""

        plan_payload = self.plan(payload)
        return json.dumps(plan_payload, sort_keys=True, separators=(",", ":"))

    def _adjust_grid_side(
        self,
        proposed_params: dict[str, Any],
        params_spec: dict[str, dict[str, Any]],
        *,
        iteration: int,
    ) -> None:
        if not {"nx", "ny", "nz"}.issubset(set(params_spec)):
            return

        nx_values = _discrete_values(params_spec["nx"])
        ny_values = _discrete_values(params_spec["ny"])
        nz_values = _discrete_values(params_spec["nz"])
        if not nx_values or nx_values != ny_values or nx_values != nz_values:
            return

        index = _discrete_index(self.seed, iteration, "grid_side", len(nx_values))
        side = nx_values[index]
        proposed_params["nx"] = int(side)
        proposed_params["ny"] = int(side)
        proposed_params["nz"] = int(side)

    def _adjust_init_redshift(
        self,
        proposed_params: dict[str, Any],
        params_spec: dict[str, dict[str, Any]],
        *,
        summary: SummarySpecV0,
        iteration: int,
        state: Mapping[str, Any],
    ) -> None:
        spec = params_spec.get("Init_redshift")
        if spec is None:
            return

        trend = str(summary.state_patch.get("trend", "insufficient_data"))
        previous = _previous_init_redshift(state)
        base = previous if previous is not None else float(proposed_params.get("Init_redshift", 0.0))

        direction = 0
        if trend == "up":
            direction = 1
        elif trend == "down":
            direction = -1
        elif trend == "flat":
            direction = 0
        else:
            direction = _signed_unit(self.seed, iteration, "init_redshift_dir")
            if direction == 0:
                direction = 1

        step_from_bounds = _step_value(spec)
        step = step_from_bounds if step_from_bounds is not None and step_from_bounds > 0 else self.init_redshift_step
        candidate = base + (direction * step)

        low = _as_float(spec.get("min"), default=None)
        high = _as_float(spec.get("max"), default=None)
        if low is not None and candidate < low:
            candidate = low
        if high is not None and candidate > high:
            candidate = high

        proposed_params["Init_redshift"] = round(float(candidate), 8)


def _coerce_summary(raw_summary: Any) -> SummarySpecV0:
    if isinstance(raw_summary, Mapping) and "version" in raw_summary:
        return validate_summary_spec(raw_summary)
    return validate_summary_spec(
        {
            "version": "v0",
            "summary_text": "no summary provided",
            "should_stop": False,
            "termination_reason_hint": None,
            "observations": [],
            "state_patch": {},
            "confidence": 0.0,
        }
    )


def _parameter_specs(param_space: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    parameters = param_space.get("parameters")
    if not isinstance(parameters, list):
        raise ValueError("param_space.parameters must be a list")

    specs: dict[str, dict[str, Any]] = {}
    for item in parameters:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        bounds_raw = item.get("bounds")
        bounds = dict(bounds_raw) if isinstance(bounds_raw, Mapping) else {}
        specs[name] = {
            "name": name,
            "type": str(item.get("type", "float")),
            "default": item.get("default"),
            "min": bounds.get("min"),
            "max": bounds.get("max"),
            "step": bounds.get("step"),
        }
    if not specs:
        raise ValueError("param_space has no usable parameter definitions")
    return specs


def _default_params(specs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, spec in specs.items():
        value = spec.get("default")
        param_type = str(spec.get("type", "float"))
        if param_type == "int":
            out[name] = int(_as_int(value, default=0))
        else:
            out[name] = round(float(_as_float(value, default=0.0)), 8)
    return out


def _previous_init_redshift(state: Mapping[str, Any]) -> float | None:
    last_plan_raw = state.get("last_plan")
    if not isinstance(last_plan_raw, Mapping):
        return None
    metadata_raw = last_plan_raw.get("metadata")
    if not isinstance(metadata_raw, Mapping):
        return None
    params_raw = metadata_raw.get("proposed_params")
    if not isinstance(params_raw, Mapping):
        return None
    value = params_raw.get("Init_redshift")
    if _is_number(value):
        return float(value)
    return None


def _discrete_values(spec: Mapping[str, Any]) -> list[int]:
    low = _as_int(spec.get("min"), default=None)
    high = _as_int(spec.get("max"), default=None)
    step = _as_int(spec.get("step"), default=1)
    if low is None or high is None or step <= 0 or low > high:
        return []
    out: list[int] = []
    value = low
    while value <= high:
        out.append(value)
        value += step
    return out


def _step_value(spec: Mapping[str, Any]) -> float | None:
    value = spec.get("step")
    if _is_number(value):
        return float(value)
    return None


def _discrete_index(seed: int, iteration: int, key: str, count: int) -> int:
    if count <= 1:
        return 0
    raw = _u01(seed, iteration, key)
    idx = int(raw * count)
    if idx < 0:
        return 0
    if idx >= count:
        return count - 1
    return idx


def _signed_unit(seed: int, iteration: int, key: str) -> int:
    value = _u01(seed, iteration, key)
    if value < (1.0 / 3.0):
        return -1
    if value < (2.0 / 3.0):
        return 0
    return 1


def _u01(seed: int, iteration: int, key: str) -> float:
    payload = f"{seed}:{iteration}:{key}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    n = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return n / float(2**64)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: Any, *, default: int | None) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _as_float(value: Any, *, default: float | None) -> float | None:
    if _is_number(value):
        return float(value)
    return default


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _format_scalar(value: Any) -> str:
    if _is_number(value):
        return f"{float(value):.6f}"
    return "none"


__all__ = ["MockPlannerAgent"]
