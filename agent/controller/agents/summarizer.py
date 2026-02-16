"""Deterministic mock summarizer agent for Phase 3B advisory flow."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from agent.controller.specs import SummarySpecV0, validate_summary_spec


@dataclass(frozen=True)
class MockSummarizerAgent:
    """Summarize last-K history records with deterministic trend heuristics."""

    lookback_k: int = 5
    flat_tol: float = 1e-6

    def summarize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a SummarySpecV0 payload from advisory inputs."""

        if self.lookback_k < 1:
            raise ValueError("lookback_k must be >= 1")
        if self.flat_tol < 0:
            raise ValueError("flat_tol must be >= 0")

        payload_map = dict(payload)
        iteration = _as_int(payload_map.get("iteration"), default=0)
        budgets = _coerce_mapping(payload_map.get("budgets"))
        state = _coerce_mapping(payload_map.get("state"))

        history_records = _extract_history_records(payload=payload_map, state=state)
        iteration_records = [record for record in history_records if record.get("record_type") == "iteration"]
        recent_records = iteration_records[-self.lookback_k :]
        recent_scalars = _extract_recent_scalars(recent_records)
        all_scalars = _extract_recent_scalars(iteration_records)

        best_scalar = max(all_scalars) if all_scalars else None
        latest_scalar = recent_scalars[-1] if recent_scalars else None
        trend = _trend_label(recent_scalars, flat_tol=self.flat_tol)

        should_stop = False
        termination_reason_hint: str | None = None
        if _is_converged(recent_scalars, flat_tol=self.flat_tol):
            should_stop = True
            termination_reason_hint = "converged_recent_trend"

        remaining_iterations = _as_int(budgets.get("remaining_iterations"), default=1)
        if remaining_iterations <= 0:
            should_stop = True
            termination_reason_hint = "max_iterations_reached"

        summary_text = (
            f"iter={iteration} recent_n={len(recent_records)} "
            f"best_scalar={_format_scalar(best_scalar)} trend={trend}"
        )
        observations = [
            f"history_records_considered={len(recent_records)}",
            f"numeric_scalars_recent={len(recent_scalars)}",
            f"best_scalar={_format_scalar(best_scalar)}",
            f"trend={trend}",
        ]

        state_patch: dict[str, Any] = {
            "summary_iteration": iteration,
            "history_window_size": len(recent_records),
            "recent_scalar_values": recent_scalars,
            "latest_scalar": latest_scalar,
            "best_scalar": best_scalar,
            "trend": trend,
        }
        confidence = _confidence(recent_scalars, lookback_k=self.lookback_k)

        raw_summary = {
            "version": "v0",
            "summary_text": summary_text,
            "should_stop": should_stop,
            "termination_reason_hint": termination_reason_hint,
            "observations": observations,
            "state_patch": state_patch,
            "confidence": confidence,
        }
        validated: SummarySpecV0 = validate_summary_spec(raw_summary)
        return validated.to_dict()

    def summarize_json(self, payload: Mapping[str, Any]) -> str:
        """Return canonical JSON output for deterministic comparisons."""

        summary = self.summarize(payload)
        return json.dumps(summary, sort_keys=True, separators=(",", ":"))


def _extract_history_records(*, payload: Mapping[str, Any], state: Mapping[str, Any]) -> list[dict[str, Any]]:
    if "history_records" in payload and isinstance(payload.get("history_records"), list):
        return _coerce_records(payload.get("history_records"))

    if "history_records" in state and isinstance(state.get("history_records"), list):
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


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _coerce_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            records.append(dict(item))
    return records


def _extract_recent_scalars(records: list[Mapping[str, Any]]) -> list[float]:
    scalars: list[float] = []
    for record in records:
        scalar = _extract_scalar(record)
        if scalar is not None:
            scalars.append(scalar)
    return scalars


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
            scalar_alt = result.get("metric_value")
            if _is_number(scalar_alt):
                return float(scalar_alt)
    return None


def _trend_label(values: list[float], *, flat_tol: float) -> str:
    if len(values) < 2:
        return "insufficient_data"
    delta = values[-1] - values[0]
    if abs(delta) <= flat_tol:
        return "flat"
    return "up" if delta > 0 else "down"


def _is_converged(values: list[float], *, flat_tol: float) -> bool:
    if len(values) < 3:
        return False
    span = max(values) - min(values)
    return abs(span) <= flat_tol


def _confidence(values: list[float], *, lookback_k: int) -> float:
    if lookback_k < 1:
        return 0.0
    ratio = len(values) / float(lookback_k)
    if ratio < 0.0:
        return 0.0
    if ratio > 1.0:
        return 1.0
    return round(ratio, 6)


def _format_scalar(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{value:.6f}"


def _as_int(value: Any, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


__all__ = ["MockSummarizerAgent"]
