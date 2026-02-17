"""Deterministic stub data generator."""

from __future__ import annotations

from typing import Any

from kernel.stub_tools.common import (
    deterministic_series,
    normalize_stub_payload,
    run_id_for_payload,
    series_checksum,
)


def _failed_result(error: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "run_id": "",
        "series": [],
        "series_checksum": "",
        "error": error,
    }


def stub_generate(payload: dict[str, Any]) -> dict[str, Any]:
    """Generate deterministic numeric series for stub domain workflows."""

    try:
        normalized = normalize_stub_payload(payload, allow_scale=False)
        seed = int(normalized["seed"])
        count = int(normalized["count"])
        offset = float(normalized["offset"])
        values = deterministic_series(seed=seed, count=count, offset=offset)
        run_id = run_id_for_payload(seed=seed, count=count, offset=offset)
        return {
            "status": "success",
            "run_id": run_id,
            "series": values,
            "series_checksum": series_checksum(values),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return _failed_result(str(exc))
