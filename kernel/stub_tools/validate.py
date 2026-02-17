"""Deterministic stub input validator."""

from __future__ import annotations

from typing import Any

from kernel.stub_tools.common import normalize_stub_payload


def _failed_result(error: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "valid": False,
        "errors": [error],
        "normalized": {
            "seed": 0,
            "count": 1,
            "offset": 0.0,
            "scale": 1.0,
        },
        "error": error,
    }


def stub_validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate payload shape and numeric constraints for stub tools."""

    try:
        normalized = normalize_stub_payload(payload, allow_scale=True)
    except Exception as exc:  # noqa: BLE001
        return _failed_result(str(exc))

    return {
        "status": "success",
        "valid": True,
        "errors": [],
        "normalized": normalized,
        "error": "",
    }
