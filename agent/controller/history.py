"""Append-only history writer and stable record schema for the controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from agent.controller.specs import ensure_json_serializable, validate_json_schema


HISTORY_RECORD_V0_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "hybrid_controller_history_record_v0",
    "type": "object",
    "properties": {
        "schema_version": {"const": "v0"},
        "record_type": {"type": "string", "enum": ["iteration", "run_end"]},
        "timestamp_utc": {"type": "string", "minLength": 1},
        "controller_run_id": {"type": "string", "minLength": 1},
        "iteration": {"type": ["integer", "null"], "minimum": 0},
        "status": {"type": "string", "enum": ["success", "failed", "terminated"]},
        "RUN_ID": {"type": ["string", "null"]},
        "summary": {"type": ["object", "null"]},
        "plan": {"type": ["object", "null"]},
        "tool_results": {"type": "array", "items": {"type": "object"}},
        "artifact_paths": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "budgets": {"type": "object"},
        "termination_reason": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
    },
    "required": [
        "schema_version",
        "record_type",
        "timestamp_utc",
        "controller_run_id",
        "iteration",
        "status",
        "RUN_ID",
        "summary",
        "plan",
        "tool_results",
        "artifact_paths",
        "budgets",
        "termination_reason",
        "error",
    ],
    "additionalProperties": False,
}


def utc_now() -> str:
    """Return a UTC timestamp in RFC-3339 basic format used by run_agent."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class HistoryRecordV0:
    """Normalized JSONL history record written by the hybrid controller."""

    schema_version: str
    record_type: str
    timestamp_utc: str
    controller_run_id: str
    iteration: int | None
    status: str
    run_id: str | None = None
    summary: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    artifact_paths: dict[str, str] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    termination_reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_type": self.record_type,
            "timestamp_utc": self.timestamp_utc,
            "controller_run_id": self.controller_run_id,
            "iteration": self.iteration,
            "status": self.status,
            "RUN_ID": self.run_id,
            "summary": dict(self.summary) if self.summary is not None else None,
            "plan": dict(self.plan) if self.plan is not None else None,
            "tool_results": [dict(item) for item in self.tool_results],
            "artifact_paths": dict(self.artifact_paths),
            "budgets": dict(self.budgets),
            "termination_reason": self.termination_reason,
            "error": self.error,
        }


def validate_history_record(payload: Mapping[str, Any]) -> HistoryRecordV0:
    """Validate and normalize one history record against HISTORY_RECORD_V0_SCHEMA."""

    if not isinstance(payload, Mapping):
        raise TypeError("history record must be a JSON object")

    payload_dict = dict(payload)
    validate_json_schema(payload_dict, HISTORY_RECORD_V0_SCHEMA, schema_name="HistoryRecordV0")
    ensure_json_serializable(payload_dict, label="HistoryRecordV0 payload")

    raw_artifact_paths = payload_dict.get("artifact_paths", {})
    artifact_paths = {str(key): str(value) for key, value in dict(raw_artifact_paths).items()}

    raw_tool_results = payload_dict.get("tool_results", [])
    tool_results = [dict(item) for item in raw_tool_results]

    normalized = HistoryRecordV0(
        schema_version=str(payload_dict["schema_version"]),
        record_type=str(payload_dict["record_type"]),
        timestamp_utc=str(payload_dict["timestamp_utc"]),
        controller_run_id=str(payload_dict["controller_run_id"]),
        iteration=payload_dict["iteration"],
        status=str(payload_dict["status"]),
        run_id=payload_dict.get("RUN_ID"),
        summary=dict(payload_dict["summary"]) if payload_dict["summary"] is not None else None,
        plan=dict(payload_dict["plan"]) if payload_dict["plan"] is not None else None,
        tool_results=tool_results,
        artifact_paths=artifact_paths,
        budgets=dict(payload_dict.get("budgets", {})),
        termination_reason=payload_dict.get("termination_reason"),
        error=payload_dict.get("error"),
    )
    ensure_json_serializable(normalized.to_dict(), label="HistoryRecordV0 normalized payload")
    return normalized


class HistoryWriter:
    """Append-only writer for controller `history.jsonl` records."""

    def __init__(self, history_path: Path | str) -> None:
        self.path = Path(history_path)

    def append(self, record: HistoryRecordV0 | Mapping[str, Any]) -> dict[str, Any]:
        """Append one validated record and return the normalized payload."""

        normalized = record if isinstance(record, HistoryRecordV0) else validate_history_record(record)
        payload = normalized.to_dict()
        ensure_json_serializable(payload, label="HistoryWriter record")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        return payload


__all__ = [
    "HISTORY_RECORD_V0_SCHEMA",
    "HistoryRecordV0",
    "HistoryWriter",
    "utc_now",
    "validate_history_record",
]
