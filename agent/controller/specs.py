"""Draft-07 schemas and dataclasses for controller advisory payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping


SUMMARY_SPEC_V0_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "summary_spec_v0",
    "type": "object",
    "properties": {
        "version": {"const": "v0"},
        "summary_text": {"type": "string", "minLength": 1},
        "should_stop": {"type": "boolean"},
        "termination_reason_hint": {"type": ["string", "null"]},
        "observations": {"type": "array", "items": {"type": "string"}},
        "state_patch": {"type": "object", "additionalProperties": True},
        "confidence": {"type": ["number", "null"], "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["version", "summary_text", "should_stop"],
    "additionalProperties": False,
}


PLAN_SPEC_V0_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "plan_spec_v0",
    "type": "object",
    "properties": {
        "version": {"const": "v0"},
        "should_stop": {"type": "boolean"},
        "termination_reason": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
        "metadata": {"type": "object", "additionalProperties": True},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "minLength": 1},
                    "payload": {"type": "object"},
                    "note": {"type": ["string", "null"]},
                },
                "required": ["tool", "payload"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["version", "tool_calls", "should_stop"],
    "additionalProperties": False,
}


class SchemaValidationError(ValueError):
    """Raised when a payload fails schema validation."""


@dataclass(frozen=True)
class SummarySpecV0:
    """Validated summary payload emitted by the summarizer advisory agent."""

    version: str
    summary_text: str
    should_stop: bool
    termination_reason_hint: str | None = None
    observations: list[str] = field(default_factory=list)
    state_patch: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "summary_text": self.summary_text,
            "should_stop": self.should_stop,
            "termination_reason_hint": self.termination_reason_hint,
            "observations": list(self.observations),
            "state_patch": dict(self.state_patch),
            "confidence": self.confidence,
        }
        return payload


@dataclass(frozen=True)
class PlanToolCallV0:
    """One planned tool invocation emitted by the planner advisory agent."""

    tool: str
    payload: dict[str, Any]
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "payload": dict(self.payload),
            "note": self.note,
        }


@dataclass(frozen=True)
class PlanSpecV0:
    """Validated plan payload emitted by the planner advisory agent."""

    version: str
    tool_calls: list[PlanToolCallV0]
    should_stop: bool
    termination_reason: str | None = None
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "tool_calls": [entry.to_dict() for entry in self.tool_calls],
            "should_stop": self.should_stop,
            "termination_reason": self.termination_reason,
            "rationale": self.rationale,
            "metadata": dict(self.metadata),
        }
        return payload


def ensure_json_serializable(payload: Any, *, label: str) -> None:
    """Raise a TypeError if a payload cannot be serialized as JSON."""

    try:
        json.dumps(payload)
    except TypeError as exc:  # pragma: no cover - defensive branch
        raise TypeError(f"{label} must be JSON-serializable: {exc}") from exc


def validate_json_schema(payload: Any, schema: Mapping[str, Any], *, schema_name: str) -> None:
    """Validate payload against a small Draft-07 subset used by controller specs."""

    errors = list(_iter_schema_errors(payload=payload, schema=schema, path="$"))
    if errors:
        raise SchemaValidationError(f"{schema_name} validation failed: {errors[0]}")


def validate_summary_spec(payload: Mapping[str, Any]) -> SummarySpecV0:
    """Validate and normalize a SummarySpec v0 payload."""

    if not isinstance(payload, Mapping):
        raise SchemaValidationError("SummarySpecV0 payload must be an object")

    payload_dict = dict(payload)
    validate_json_schema(payload_dict, SUMMARY_SPEC_V0_SCHEMA, schema_name="SummarySpecV0")
    ensure_json_serializable(payload_dict, label="SummarySpecV0 payload")

    observations_raw = payload_dict.get("observations", [])
    state_patch_raw = payload_dict.get("state_patch", {})
    confidence_raw = payload_dict.get("confidence")
    confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None

    summary = SummarySpecV0(
        version=str(payload_dict["version"]),
        summary_text=str(payload_dict["summary_text"]),
        should_stop=bool(payload_dict["should_stop"]),
        termination_reason_hint=payload_dict.get("termination_reason_hint"),
        observations=[str(item) for item in observations_raw],
        state_patch=dict(state_patch_raw),
        confidence=confidence,
    )
    ensure_json_serializable(summary.to_dict(), label="SummarySpecV0 normalized payload")
    return summary


def validate_plan_spec(payload: Mapping[str, Any]) -> PlanSpecV0:
    """Validate and normalize a PlanSpec v0 payload."""

    if not isinstance(payload, Mapping):
        raise SchemaValidationError("PlanSpecV0 payload must be an object")

    payload_dict = dict(payload)
    validate_json_schema(payload_dict, PLAN_SPEC_V0_SCHEMA, schema_name="PlanSpecV0")
    ensure_json_serializable(payload_dict, label="PlanSpecV0 payload")

    calls: list[PlanToolCallV0] = []
    for item in payload_dict["tool_calls"]:
        entry = dict(item)
        calls.append(
            PlanToolCallV0(
                tool=str(entry["tool"]),
                payload=dict(entry["payload"]),
                note=entry.get("note"),
            )
        )

    plan = PlanSpecV0(
        version=str(payload_dict["version"]),
        tool_calls=calls,
        should_stop=bool(payload_dict["should_stop"]),
        termination_reason=payload_dict.get("termination_reason"),
        rationale=str(payload_dict.get("rationale", "")),
        metadata=dict(payload_dict.get("metadata", {})),
    )
    ensure_json_serializable(plan.to_dict(), label="PlanSpecV0 normalized payload")
    return plan


def _iter_schema_errors(*, payload: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_json_type(payload, expected_type):
        return [f"{path}: expected type {expected_type!r}, got {type(payload).__name__!r}"]

    if "const" in schema and payload != schema["const"]:
        return [f"{path}: expected const {schema['const']!r}, got {payload!r}"]

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and payload not in enum_values:
        return [f"{path}: value {payload!r} not in enum {enum_values!r}"]

    if isinstance(payload, Mapping):
        object_errors = _iter_object_errors(payload=payload, schema=schema, path=path)
        if object_errors:
            return object_errors

    if isinstance(payload, list):
        list_errors = _iter_array_errors(payload=payload, schema=schema, path=path)
        if list_errors:
            return list_errors

    scalar_errors = _iter_scalar_errors(payload=payload, schema=schema, path=path)
    if scalar_errors:
        return scalar_errors

    return []


def _iter_object_errors(*, payload: Mapping[str, Any], schema: Mapping[str, Any], path: str) -> list[str]:
    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if key not in payload:
                return [f"{path}: missing required key {key!r}"]

    properties = schema.get("properties")
    properties_map = properties if isinstance(properties, Mapping) else {}
    additional = schema.get("additionalProperties", True)

    for key, value in payload.items():
        if key in properties_map:
            nested_errors = _iter_schema_errors(
                payload=value,
                schema=dict(properties_map[key]),
                path=f"{path}.{key}",
            )
            if nested_errors:
                return nested_errors
            continue

        if additional is False:
            return [f"{path}: unexpected key {key!r}"]
        if isinstance(additional, Mapping):
            nested_errors = _iter_schema_errors(
                payload=value,
                schema=dict(additional),
                path=f"{path}.{key}",
            )
            if nested_errors:
                return nested_errors
    return []


def _iter_array_errors(*, payload: list[Any], schema: Mapping[str, Any], path: str) -> list[str]:
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and len(payload) < min_items:
        return [f"{path}: expected at least {min_items} items, got {len(payload)}"]
    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and len(payload) > max_items:
        return [f"{path}: expected at most {max_items} items, got {len(payload)}"]

    item_schema = schema.get("items")
    if isinstance(item_schema, Mapping):
        for idx, item in enumerate(payload):
            nested_errors = _iter_schema_errors(
                payload=item,
                schema=dict(item_schema),
                path=f"{path}[{idx}]",
            )
            if nested_errors:
                return nested_errors
    return []


def _iter_scalar_errors(*, payload: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if isinstance(payload, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(payload) < min_length:
            return [f"{path}: expected minLength {min_length}, got {len(payload)}"]
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(payload) > max_length:
            return [f"{path}: expected maxLength {max_length}, got {len(payload)}"]

    if _is_number(payload):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and payload < minimum:
            return [f"{path}: expected minimum {minimum}, got {payload}"]
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and payload > maximum:
            return [f"{path}: expected maximum {maximum}, got {payload}"]
    return []


def _matches_json_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_matches_json_type(value, option) for option in expected)
    if not isinstance(expected, str):
        return False
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_number(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, Mapping)
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


__all__ = [
    "PLAN_SPEC_V0_SCHEMA",
    "SUMMARY_SPEC_V0_SCHEMA",
    "PlanSpecV0",
    "PlanToolCallV0",
    "SchemaValidationError",
    "SummarySpecV0",
    "ensure_json_serializable",
    "validate_json_schema",
    "validate_plan_spec",
    "validate_summary_spec",
]
