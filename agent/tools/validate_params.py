"""Pure MCP-style wrapper for parameter validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.validate_params import ValidationError
from agent.validate_params import _load_yaml_file as _core_load_yaml_file
from agent.validate_params import validate_params as _core_validate_params


ALLOWED_INPUT_KEYS = {"params"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_result() -> dict[str, Any]:
    return {
        "valid": False,
        "errors": [],
    }


def _parse_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []

    unknown_keys = sorted(set(payload.keys()) - ALLOWED_INPUT_KEYS)
    if unknown_keys:
        errors.append(f"unknown payload keys: {unknown_keys}")

    params = payload.get("params")
    if not isinstance(params, dict):
        errors.append("params must be an object")
        return ({}, errors)

    return (params, errors)


def validate_params(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate params against the canonical agent spec with no side effects."""

    result = _default_result()

    try:
        if not isinstance(payload, dict):
            result["errors"] = ["payload must be a JSON object"]
            return result

        params, errors = _parse_payload(payload)
        if errors:
            result["errors"] = sorted(errors)
            return result

        spec_path = _repo_root() / "agent" / "spec" / "param_space_v0.yaml"
        spec = _core_load_yaml_file(spec_path)
        _core_validate_params(spec=spec, params=params)

        return {"valid": True, "errors": []}
    except (ValidationError, FileNotFoundError) as exc:
        result["errors"] = [str(exc)]
        return result
    except Exception as exc:  # noqa: BLE001
        result["errors"] = [str(exc)]
        return result


__all__ = ["validate_params"]

