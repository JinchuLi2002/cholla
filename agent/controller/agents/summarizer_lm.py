"""LM-backed summarizer agent that emits normalized SummarySpec payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping

from agent.controller.lm_client import LMClientError, call_llm_json_retry
from agent.controller.specs import (
    SUMMARIZER_OUTPUT_V0_SCHEMA,
    SchemaValidationError,
    SummarySpecV0,
    ensure_json_serializable,
    validate_json_schema,
    validate_summary_spec,
)

SUMMARIZER_LM_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "summarizer_lm_input_v0",
    "type": "object",
    "properties": {
        "iteration_index": {"type": "integer", "minimum": 0},
        "last_k_history_rows": {"type": "array", "items": {"type": "object"}},
        "trend_summary": {
            "type": "object",
            "properties": {
                "trend": {"type": "string", "minLength": 1},
                "best_scalar": {"type": ["number", "null"]},
                "latest_scalar": {"type": ["number", "null"]},
                "recent_scalar_values": {"type": "array", "items": {"type": "number"}},
                "history_window_size": {"type": "integer", "minimum": 0},
            },
            "required": [
                "trend",
                "best_scalar",
                "latest_scalar",
                "recent_scalar_values",
                "history_window_size",
            ],
            "additionalProperties": False,
        },
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
        "param_space": {"type": "object", "additionalProperties": True},
    },
    "required": [
        "iteration_index",
        "last_k_history_rows",
        "trend_summary",
        "budget_remaining",
        "param_space",
    ],
    "additionalProperties": False,
}

DEFAULT_SUMMARIZER_SYSTEM_PROMPT = (
    "You are the Summarizer module for a hybrid simulation controller. "
    "Return only JSON that matches the required output schema. "
    "Never emit markdown or explanatory prose. "
    "Use only the provided last_k_history_rows and trend_summary. "
    "Set should_stop true only when termination is recommended by evidence or budget."
)


class SummarizerLMError(RuntimeError):
    """Raised when summarizer LM input/output handling fails."""


def _err(code: str, detail: str) -> str:
    return f"summarizer_lm_error[{code}]: {detail}"


@dataclass(frozen=True)
class SummarizerLMAgent:
    """Call LM with strict JSON output and normalize into SummarySpecV0."""

    model: str = "gpt-4o-mini"
    temperature: float = 1.0
    max_attempts: int = 5
    stability_attempts: int | None = None
    seed: int | None = None
    lookback_k: int = 5
    flat_tol: float = 1e-6
    system_prompt: str = DEFAULT_SUMMARIZER_SYSTEM_PROMPT
    last_lm_trace: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    def summarize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        summarizer_input = self._normalize_input(payload)
        max_attempts = self._resolve_max_attempts()

        try:
            retry_result = call_llm_json_retry(
                model=self.model,
                system_prompt=self.system_prompt,
                user_payload=summarizer_input,
                temperature=self.temperature,
                output_schema=SUMMARIZER_OUTPUT_V0_SCHEMA,
                attempts=max_attempts,
                seed=self.seed,
            )
            lm_output_raw = retry_result.get("output")
            if not isinstance(lm_output_raw, Mapping):
                raise SummarizerLMError(_err("invalid_lm_output", "retry output missing object payload"))
            lm_output = dict(lm_output_raw)
            self._set_trace(
                {
                    "live_lm_used": True,
                    "status": "ok",
                    "error": None,
                    "model": retry_result.get("model", self.model),
                    "temperature": retry_result.get("temperature", self.temperature),
                    "seed": retry_result.get("seed", self.seed),
                    "provider": retry_result.get("provider"),
                    "base_url": retry_result.get("base_url"),
                    "api_version": retry_result.get("api_version"),
                    "raw_outputs": list(retry_result.get("raw_outputs", [])),
                    "attempt_summaries": list(retry_result.get("attempt_summaries", [])),
                    "accepted_attempt_index": retry_result.get("accepted_attempt_index"),
                    "max_attempts": retry_result.get("max_attempts", max_attempts),
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
                    "seed": self.seed,
                    "provider": None,
                    "base_url": None,
                    "api_version": None,
                    "raw_outputs": list(getattr(exc, "raw_outputs", [])),
                    "attempt_summaries": list(getattr(exc, "attempt_summaries", [])),
                    "accepted_attempt_index": None,
                    "max_attempts": max_attempts,
                }
            )
            raise SummarizerLMError(_err("lm_call_failed", str(exc))) from exc

        try:
            validate_json_schema(lm_output, SUMMARIZER_OUTPUT_V0_SCHEMA, schema_name="SummarizerLMOutputV0")
            ensure_json_serializable(lm_output, label="SummarizerLMOutputV0 payload")
            validated: SummarySpecV0 = validate_summary_spec(lm_output)
        except (SchemaValidationError, TypeError) as exc:
            raise SummarizerLMError(_err("invalid_lm_output", str(exc))) from exc
        return validated.to_dict()

    def summarize_json(self, payload: Mapping[str, Any]) -> str:
        """Return canonical JSON output for deterministic comparisons."""

        summary = self.summarize(payload)
        return json.dumps(summary, sort_keys=True, separators=(",", ":"))

    def _normalize_input(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.lookback_k < 1:
            raise SummarizerLMError(_err("invalid_lookback_k", "lookback_k must be >= 1"))
        if self.flat_tol < 0:
            raise SummarizerLMError(_err("invalid_flat_tol", "flat_tol must be >= 0"))
        if not isinstance(payload, Mapping):
            raise SummarizerLMError(_err("invalid_input_type", "summarizer payload must be an object"))

        payload_map = dict(payload)
        if self._looks_like_structured_payload(payload_map):
            candidate = self._structured_payload(payload_map)
        else:
            candidate = self._from_legacy_payload(payload_map)

        try:
            validate_json_schema(candidate, SUMMARIZER_LM_INPUT_SCHEMA, schema_name="SummarizerLMInputV0")
            ensure_json_serializable(candidate, label="SummarizerLMInputV0 payload")
        except (SchemaValidationError, TypeError) as exc:
            raise SummarizerLMError(_err("invalid_input", str(exc))) from exc

        return candidate

    def _looks_like_structured_payload(self, payload: Mapping[str, Any]) -> bool:
        required = {
            "iteration_index",
            "last_k_history_rows",
            "trend_summary",
            "budget_remaining",
            "param_space",
        }
        return required.issubset(set(payload.keys()))

    def _structured_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows_raw = payload.get("last_k_history_rows")
        if not isinstance(rows_raw, list):
            raise SummarizerLMError(_err("last_k_history_rows", "last_k_history_rows must be an array"))
        trend_summary_raw = payload.get("trend_summary")
        if not isinstance(trend_summary_raw, Mapping):
            raise SummarizerLMError(_err("trend_summary", "trend_summary must be an object"))
        budget_raw = payload.get("budget_remaining")
        if not isinstance(budget_raw, Mapping):
            raise SummarizerLMError(_err("budget_remaining", "budget_remaining must be an object"))
        param_space_raw = payload.get("param_space")
        if not isinstance(param_space_raw, Mapping):
            raise SummarizerLMError(_err("param_space", "param_space must be an object"))

        return {
            "iteration_index": payload.get("iteration_index"),
            "last_k_history_rows": _coerce_records(rows_raw),
            "trend_summary": dict(trend_summary_raw),
            "budget_remaining": {
                "remaining_iterations": budget_raw.get("remaining_iterations"),
                "remaining_tool_calls": budget_raw.get("remaining_tool_calls"),
                "remaining_walltime_sec": budget_raw.get("remaining_walltime_sec"),
            },
            "param_space": dict(param_space_raw),
        }

    def _from_legacy_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        iteration_index = _as_non_negative_int(payload.get("iteration"), default=0)
        state = dict(payload.get("state")) if isinstance(payload.get("state"), Mapping) else {}
        budgets = dict(payload.get("budgets")) if isinstance(payload.get("budgets"), Mapping) else {}
        param_space = dict(payload.get("param_space")) if isinstance(payload.get("param_space"), Mapping) else {}

        history_records = _extract_history_records(payload=payload, state=state)
        iteration_records = [record for record in history_records if record.get("record_type") == "iteration"]
        recent_records = iteration_records[-self.lookback_k :]
        recent_scalars = _extract_recent_scalars(recent_records)
        all_scalars = _extract_recent_scalars(iteration_records)

        trend_summary = {
            "trend": _trend_label(recent_scalars, flat_tol=self.flat_tol),
            "best_scalar": max(all_scalars) if all_scalars else None,
            "latest_scalar": recent_scalars[-1] if recent_scalars else None,
            "recent_scalar_values": recent_scalars,
            "history_window_size": len(recent_records),
        }

        return {
            "iteration_index": iteration_index,
            "last_k_history_rows": recent_records,
            "trend_summary": trend_summary,
            "budget_remaining": {
                "remaining_iterations": _as_non_negative_int(budgets.get("remaining_iterations"), default=0),
                "remaining_tool_calls": _as_non_negative_int(budgets.get("remaining_tool_calls"), default=0),
                "remaining_walltime_sec": _as_non_negative_float_or_none(
                    budgets.get("remaining_walltime_sec")
                ),
            },
            "param_space": param_space,
        }

    def _set_trace(self, trace: Mapping[str, Any]) -> None:
        object.__setattr__(self, "last_lm_trace", dict(trace))

    def _resolve_max_attempts(self) -> int:
        raw_attempts = self.max_attempts if self.stability_attempts is None else self.stability_attempts
        if not isinstance(raw_attempts, int) or raw_attempts < 1:
            raise SummarizerLMError(_err("invalid_attempts", "max_attempts must be an integer >= 1"))
        return raw_attempts


def _extract_history_records(*, payload: Mapping[str, Any], state: Mapping[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("history_records"), list):
        return _coerce_records(payload.get("history_records"))
    if isinstance(state.get("history_records"), list):
        return _coerce_records(state.get("history_records"))

    path_value = payload.get("history_path")
    if not isinstance(path_value, str) or not path_value.strip():
        path_value = state.get("history_path")
    if isinstance(path_value, str) and path_value.strip():
        return _read_history_jsonl(Path(path_value.strip()))

    return []


def _read_history_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if isinstance(payload, Mapping):
            records.append(dict(payload))
    return records


def _coerce_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _extract_recent_scalars(records: list[Mapping[str, Any]]) -> list[float]:
    out: list[float] = []
    for record in records:
        scalar = _extract_scalar(record)
        if scalar is not None:
            out.append(scalar)
    return out


def _extract_scalar(record: Mapping[str, Any]) -> float | None:
    metric = record.get("metric")
    if isinstance(metric, Mapping):
        scalar = metric.get("scalar")
        if _is_number(scalar):
            return float(scalar)

    tool_results = record.get("tool_results")
    if isinstance(tool_results, list):
        for call in tool_results:
            if not isinstance(call, Mapping):
                continue
            result = call.get("result")
            if not isinstance(result, Mapping):
                continue
            scalar = result.get("scalar")
            if _is_number(scalar):
                return float(scalar)
            metric_value = result.get("metric_value")
            if _is_number(metric_value):
                return float(metric_value)
    return None


def _trend_label(values: list[float], *, flat_tol: float) -> str:
    if len(values) < 2:
        return "insufficient_data"
    delta = values[-1] - values[0]
    if abs(delta) <= flat_tol:
        return "flat"
    return "up" if delta > 0 else "down"


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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


__all__ = [
    "DEFAULT_SUMMARIZER_SYSTEM_PROMPT",
    "SUMMARIZER_LM_INPUT_SCHEMA",
    "SummarizerLMAgent",
    "SummarizerLMError",
]
