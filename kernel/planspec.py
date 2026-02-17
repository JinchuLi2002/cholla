"""PlanSpec + StepSpec models and deterministic ID helpers for kernel M1."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any


PLAN_SPEC_SCHEMA_VERSION = "v1"
STEP_SPEC_SCHEMA_VERSION = "v1"


def canonical_json(obj: Any) -> str:
    """Return canonical JSON with sorted keys and compact separators."""

    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def hash_canonical_payload(payload: Any) -> str:
    """Return SHA-256 hex digest for the canonical JSON form of payload."""

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StepSpec:
    """One deterministic plan step (typically one tool call)."""

    tool: str
    payload: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    expected_artifacts: list[str] = field(default_factory=list)
    expects_run_id: bool = False
    expects_metric_scalar: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = STEP_SPEC_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool": self.tool,
            "payload": dict(self.payload),
            "note": self.note,
            "expected_artifacts": list(self.expected_artifacts),
            "expects_run_id": self.expects_run_id,
            "expects_metric_scalar": self.expects_metric_scalar,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PlanSpec:
    """Deterministic plan representation executed by kernel."""

    steps: list[StepSpec] = field(default_factory=list)
    should_stop: bool = False
    termination_reason: str | None = None
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = PLAN_SPEC_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "steps": [step.to_dict() for step in self.steps],
            "should_stop": self.should_stop,
            "termination_reason": self.termination_reason,
            "rationale": self.rationale,
            "metadata": dict(self.metadata),
        }


def step_hash_payload(
    plan_id: str,
    idx: int,
    step: StepSpec,
    *,
    include_metadata: bool = False,
) -> dict[str, Any]:
    """Return normalized payload used to hash a step ID."""

    return {
        "schema_version": step.schema_version,
        "plan_id": plan_id,
        "step_index": idx,
        "step": _step_payload(step, include_metadata=include_metadata),
    }


def plan_hash_payload(plan: PlanSpec, *, include_metadata: bool = False) -> dict[str, Any]:
    """Return normalized payload used to hash a plan ID."""

    payload: dict[str, Any] = {
        "schema_version": plan.schema_version,
        "should_stop": plan.should_stop,
        "termination_reason": plan.termination_reason,
        "rationale": plan.rationale,
        "steps": [_step_payload(step, include_metadata=include_metadata) for step in plan.steps],
    }
    if include_metadata:
        payload["metadata"] = dict(plan.metadata)
    return payload


def compute_plan_id(plan: PlanSpec) -> str:
    """Compute deterministic plan ID (metadata excluded by default)."""

    return hash_canonical_payload(plan_hash_payload(plan))


def compute_step_id(plan_id: str, idx: int, step: StepSpec) -> str:
    """Compute deterministic step ID (metadata excluded by default)."""

    return hash_canonical_payload(step_hash_payload(plan_id, idx, step))


def _step_payload(step: StepSpec, *, include_metadata: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": step.schema_version,
        "tool": step.tool,
        "payload": dict(step.payload),
        "note": step.note,
        "expected_artifacts": list(step.expected_artifacts),
        "expects_run_id": step.expects_run_id,
        "expects_metric_scalar": step.expects_metric_scalar,
    }
    if include_metadata:
        payload["metadata"] = dict(step.metadata)
    return payload


__all__ = [
    "PLAN_SPEC_SCHEMA_VERSION",
    "STEP_SPEC_SCHEMA_VERSION",
    "PlanSpec",
    "StepSpec",
    "canonical_json",
    "hash_canonical_payload",
    "plan_hash_payload",
    "step_hash_payload",
    "compute_plan_id",
    "compute_step_id",
]
