"""Deterministic scalar metric for stub toolpack."""

from __future__ import annotations

from typing import Any

from kernel.stub_tools.common import deterministic_series, normalize_stub_payload, run_id_for_payload


METRIC_NAME = "stub_scalar_v1"


def _failed_result(error: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "metric_name": METRIC_NAME,
        "scalar": None,
        "details": {
            "status": "failed",
            "formula": "scalar = mean(series) * scale",
            "run_id": "",
            "count": 0,
            "seed": 0,
            "scale": 1.0,
        },
        "error": error,
    }


def stub_metric(payload: dict[str, Any]) -> dict[str, Any]:
    """Compute deterministic scalar from deterministic generated series."""

    try:
        normalized = normalize_stub_payload(payload, allow_scale=True)
        seed = int(normalized["seed"])
        count = int(normalized["count"])
        offset = float(normalized["offset"])
        scale = float(normalized["scale"])
        series = deterministic_series(seed=seed, count=count, offset=offset)
        mean = sum(series) / float(len(series))
        scalar = round(mean * scale, 6)
        return {
            "status": "success",
            "metric_name": METRIC_NAME,
            "scalar": scalar,
            "details": {
                "status": "success",
                "formula": "scalar = mean(series) * scale",
                "run_id": run_id_for_payload(seed=seed, count=count, offset=offset),
                "count": count,
                "seed": seed,
                "scale": scale,
            },
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return _failed_result(str(exc))
