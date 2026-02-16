"""LM-backed planner agent that emits normalized PlanSpec payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping

from agent.controller.lm_client import LMClientError, call_llm_json_stable
from agent.controller.specs import (
    SUMMARY_SPEC_V0_SCHEMA,
    PlanSpecV0,
    SchemaValidationError,
    ensure_json_serializable,
    validate_json_schema,
    validate_plan_spec,
    validate_summary_spec,
)

PLANNER_LM_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "planner_lm_input_v0",
    "type": "object",
    "properties": {
        "summary_spec": SUMMARY_SPEC_V0_SCHEMA,
        "param_space": {"type": "object", "additionalProperties": True},
        "budget_remaining": {
            "type": "object",
            "properties": {
                "remaining_iterations": {"type": "integer", "minimum": 0},
                "remaining_tool_calls": {"type": "integer", "minimum": 0},
                "remaining_walltime_sec": {"type": ["number", "null"], "minimum": 0.0},
            },
            "required": ["remaining_iterations", "remaining_tool_calls", "remaining_walltime_sec"],
            "additionalProperties": False,
        },
        "iteration_index": {"type": "integer", "minimum": 0},
        "objective": {"type": "string", "minLength": 1},
    },
    "required": [
        "summary_spec",
        "param_space",
        "budget_remaining",
        "iteration_index",
        "objective",
    ],
    "additionalProperties": False,
}

PLANNER_LM_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "planner_lm_output_v0",
    "type": "object",
    "properties": {
        "stop_recommendation": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1},
        "termination_reason": {"type": ["string", "null"]},
        "proposals": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "params": {"type": "object"},
                    "note": {"type": ["string", "null"]},
                },
                "required": ["params"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["stop_recommendation", "reason", "proposals"],
    "additionalProperties": False,
}

DEFAULT_PLANNER_SYSTEM_PROMPT = (
    "You are the Planner module for a hybrid simulation controller. "
    "Return only JSON that matches the required output schema. "
    "Never emit markdown or explanatory prose. "
    "Recommend exactly one proposal in proposals[0].params that stays within param_space bounds. "
    "Set stop_recommendation=true only when the run should terminate."
)


class PlannerLMError(RuntimeError):
    """Raised when planner LM input/output handling fails."""


def _err(code: str, detail: str) -> str:
    return f"planner_lm_error[{code}]: {detail}"


@dataclass(frozen=True)
class PlannerLMAgent:
    """Call LM with strict JSON output and normalize into PlanSpecV0."""

    model: str = "gpt-4o-mini"
    temperature: float = 1.0
    stability_attempts: int = 2
    objective: str = "maximize_metric_scalar"
    system_prompt: str = DEFAULT_PLANNER_SYSTEM_PROMPT
    last_lm_trace: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    def plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        planner_input = self._normalize_input(payload)

        try:
            stable_result = call_llm_json_stable(
                model=self.model,
                system_prompt=self.system_prompt,
                user_payload=planner_input,
                temperature=self.temperature,
                output_schema=PLANNER_LM_OUTPUT_SCHEMA,
                attempts=self.stability_attempts,
            )
            lm_output_raw = stable_result.get("output")
            if not isinstance(lm_output_raw, Mapping):
                raise PlannerLMError(_err("invalid_lm_output", "stable output missing object payload"))
            lm_output = dict(lm_output_raw)
            self._set_trace(
                {
                    "live_lm_used": True,
                    "status": "ok",
                    "error": None,
                    "model": stable_result.get("model", self.model),
                    "temperature": stable_result.get("temperature", self.temperature),
                    "provider": stable_result.get("provider"),
                    "base_url": stable_result.get("base_url"),
                    "api_version": stable_result.get("api_version"),
                    "raw_outputs": list(stable_result.get("raw_outputs", [])),
                    "canonical_outputs": list(stable_result.get("canonical_outputs", [])),
                    "stability_check_result": dict(stable_result.get("stability_check_result", {})),
                }
            )
        except LMClientError as exc:
            self._set_trace(
                {
                    "live_lm_used": False,
                    "status": "failed",
                    "error": str(exc),
                    "model": self.model,
                    "temperature": float(self.temperature),
                    "provider": None,
                    "base_url": None,
                    "api_version": None,
                    "raw_outputs": list(getattr(exc, "raw_outputs", [])),
                    "canonical_outputs": list(getattr(exc, "canonical_outputs", [])),
                    "stability_check_result": {
                        "attempts": self.stability_attempts,
                        "passed": False,
                    },
                }
            )
            raise PlannerLMError(_err("lm_call_failed", str(exc))) from exc

        try:
            validate_json_schema(lm_output, PLANNER_LM_OUTPUT_SCHEMA, schema_name="PlannerLMOutputV0")
            ensure_json_serializable(lm_output, label="PlannerLMOutputV0 payload")
        except (SchemaValidationError, TypeError) as exc:
            raise PlannerLMError(_err("invalid_lm_output", str(exc))) from exc

        proposals_raw = lm_output.get("proposals")
        proposals = proposals_raw if isinstance(proposals_raw, list) else []
        if len(proposals) != 1:
            raise PlannerLMError(_err("proposal_count", f"expected exactly 1 proposal, got {len(proposals)}"))

        proposal_raw = proposals[0]
        if not isinstance(proposal_raw, Mapping):
            raise PlannerLMError(_err("proposal_type", "proposals[0] must be an object"))
        params_raw = proposal_raw.get("params")
        if not isinstance(params_raw, Mapping) or not params_raw:
            raise PlannerLMError(_err("proposal_params", "proposals[0].params must be a non-empty object"))
        proposed_params = dict(params_raw)

        reason = str(lm_output.get("reason", "")).strip()
        if not reason:
            raise PlannerLMError(_err("reason", "reason must be non-empty"))

        stop_recommendation = bool(lm_output.get("stop_recommendation"))
        termination_reason_raw = lm_output.get("termination_reason")
        termination_reason = str(termination_reason_raw).strip() if isinstance(termination_reason_raw, str) else ""
        if stop_recommendation and not termination_reason:
            termination_reason = "planner_stop_recommendation"

        proposal_note_raw = proposal_raw.get("note")
        proposal_note = str(proposal_note_raw) if isinstance(proposal_note_raw, str) else None

        raw_plan = {
            "version": "v0",
            "should_stop": stop_recommendation,
            "termination_reason": termination_reason or None,
            "rationale": reason,
            "metadata": {
                "proposed_params": proposed_params,
                "reason": reason,
                "stop_recommendation": stop_recommendation,
                "objective": planner_input["objective"],
                "iteration_index": planner_input["iteration_index"],
                "budget_remaining": dict(planner_input["budget_remaining"]),
            },
            "tool_calls": [
                {
                    "tool": "validate_params",
                    "payload": {"params": proposed_params},
                    "note": proposal_note,
                }
            ],
        }

        try:
            validated: PlanSpecV0 = validate_plan_spec(raw_plan)
        except (SchemaValidationError, TypeError) as exc:
            raise PlannerLMError(_err("invalid_plan_spec", str(exc))) from exc
        return validated.to_dict()

    def plan_json(self, payload: Mapping[str, Any]) -> str:
        """Return canonical JSON output for deterministic comparisons."""

        plan_payload = self.plan(payload)
        return json.dumps(plan_payload, sort_keys=True, separators=(",", ":"))

    def _normalize_input(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise PlannerLMError(_err("invalid_input_type", "planner payload must be an object"))

        payload_map = dict(payload)
        if self._looks_like_structured_payload(payload_map):
            candidate = self._structured_payload(payload_map)
        else:
            candidate = self._from_legacy_payload(payload_map)

        try:
            validate_json_schema(candidate, PLANNER_LM_INPUT_SCHEMA, schema_name="PlannerLMInputV0")
            ensure_json_serializable(candidate, label="PlannerLMInputV0 payload")
            candidate["summary_spec"] = validate_summary_spec(candidate["summary_spec"]).to_dict()
        except (SchemaValidationError, TypeError) as exc:
            raise PlannerLMError(_err("invalid_input", str(exc))) from exc

        return candidate

    def _structured_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary_raw = payload.get("summary_spec")
        if not isinstance(summary_raw, Mapping):
            raise PlannerLMError(_err("summary_spec", "summary_spec must be an object"))
        param_space_raw = payload.get("param_space")
        if not isinstance(param_space_raw, Mapping):
            raise PlannerLMError(_err("param_space", "param_space must be an object"))
        budget_raw = payload.get("budget_remaining")
        if not isinstance(budget_raw, Mapping):
            raise PlannerLMError(_err("budget_remaining", "budget_remaining must be an object"))

        return {
            "summary_spec": dict(summary_raw),
            "param_space": dict(param_space_raw),
            "budget_remaining": {
                "remaining_iterations": budget_raw.get("remaining_iterations"),
                "remaining_tool_calls": budget_raw.get("remaining_tool_calls"),
                "remaining_walltime_sec": budget_raw.get("remaining_walltime_sec"),
            },
            "iteration_index": payload.get("iteration_index"),
            "objective": payload.get("objective"),
        }

    def _from_legacy_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary_raw = payload.get("summary")
        if not isinstance(summary_raw, Mapping):
            raise PlannerLMError(
                _err(
                    "legacy_summary",
                    "planner payload missing summary_spec (or legacy summary object)",
                )
            )

        param_space_raw = payload.get("param_space")
        if not isinstance(param_space_raw, Mapping):
            raise PlannerLMError(_err("legacy_param_space", "planner payload missing param_space object"))

        budgets_raw = payload.get("budgets")
        budgets = dict(budgets_raw) if isinstance(budgets_raw, Mapping) else {}

        objective_raw = payload.get("objective")
        objective = str(objective_raw).strip() if isinstance(objective_raw, str) else self.objective
        if not objective:
            objective = self.objective

        return {
            "summary_spec": dict(summary_raw),
            "param_space": dict(param_space_raw),
            "budget_remaining": {
                "remaining_iterations": _as_non_negative_int(budgets.get("remaining_iterations"), default=0),
                "remaining_tool_calls": _as_non_negative_int(budgets.get("remaining_tool_calls"), default=0),
                "remaining_walltime_sec": _as_non_negative_float_or_none(
                    budgets.get("remaining_walltime_sec")
                ),
            },
            "iteration_index": _as_non_negative_int(payload.get("iteration"), default=0),
            "objective": objective,
        }

    def _looks_like_structured_payload(self, payload: Mapping[str, Any]) -> bool:
        required = {"summary_spec", "param_space", "budget_remaining", "iteration_index", "objective"}
        return required.issubset(set(payload.keys()))

    def _set_trace(self, trace: Mapping[str, Any]) -> None:
        object.__setattr__(self, "last_lm_trace", dict(trace))


def _as_non_negative_int(value: Any, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _as_non_negative_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        cast = float(value)
        if cast >= 0.0:
            return cast
    return None


__all__ = [
    "DEFAULT_PLANNER_SYSTEM_PROMPT",
    "PLANNER_LM_INPUT_SCHEMA",
    "PLANNER_LM_OUTPUT_SCHEMA",
    "PlannerLMAgent",
    "PlannerLMError",
]
