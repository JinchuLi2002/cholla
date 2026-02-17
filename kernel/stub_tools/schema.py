"""JSON schemas for deterministic stub tools."""

from __future__ import annotations

from typing import Any


_STUB_INPUT_COMMON: dict[str, Any] = {
    "seed": {"type": "integer", "minimum": 0},
    "count": {"type": "integer", "minimum": 1, "maximum": 4096},
    "offset": {"type": "number"},
    "scale": {"type": "number"},
}


STUB_VALIDATE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": dict(_STUB_INPUT_COMMON),
    "required": ["seed"],
    "additionalProperties": False,
}

STUB_VALIDATE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["success", "failed"]},
        "valid": {"type": "boolean"},
        "errors": {"type": "array", "items": {"type": "string"}},
        "normalized": {
            "type": "object",
            "properties": dict(_STUB_INPUT_COMMON),
            "required": ["seed", "count", "offset", "scale"],
            "additionalProperties": False,
        },
        "error": {"type": "string"},
    },
    "required": ["status", "valid", "errors", "normalized", "error"],
    "additionalProperties": False,
}


STUB_GENERATE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "seed": _STUB_INPUT_COMMON["seed"],
        "count": _STUB_INPUT_COMMON["count"],
        "offset": _STUB_INPUT_COMMON["offset"],
    },
    "required": ["seed"],
    "additionalProperties": False,
}

STUB_GENERATE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["success", "failed"]},
        "run_id": {"type": "string"},
        "series": {"type": "array", "items": {"type": "number"}},
        "series_checksum": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["status", "run_id", "series", "series_checksum", "error"],
    "additionalProperties": False,
}


STUB_METRIC_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": dict(_STUB_INPUT_COMMON),
    "required": ["seed"],
    "additionalProperties": False,
}

STUB_METRIC_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["success", "failed"]},
        "metric_name": {"type": "string"},
        "scalar": {"type": ["number", "null"]},
        "details": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "formula": {"type": "string"},
                "run_id": {"type": "string"},
                "count": {"type": "integer"},
                "seed": {"type": "integer"},
                "scale": {"type": "number"},
            },
            "required": ["status", "formula", "run_id", "count", "seed", "scale"],
            "additionalProperties": False,
        },
        "error": {"type": "string"},
    },
    "required": ["status", "metric_name", "scalar", "details", "error"],
    "additionalProperties": False,
}


__all__ = [
    "STUB_GENERATE_INPUT_SCHEMA",
    "STUB_GENERATE_OUTPUT_SCHEMA",
    "STUB_METRIC_INPUT_SCHEMA",
    "STUB_METRIC_OUTPUT_SCHEMA",
    "STUB_VALIDATE_INPUT_SCHEMA",
    "STUB_VALIDATE_OUTPUT_SCHEMA",
]
