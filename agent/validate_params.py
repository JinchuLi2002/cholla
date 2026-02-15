#!/usr/bin/env python3
"""Validate proposed Tier 4 params against a param-space spec."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

import yaml


class ValidationError(Exception):
    """Raised when validation fails."""


def _load_yaml_file(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValidationError(f"spec root must be a mapping: {path}")
    return payload


def _parse_params_arg(raw: str) -> dict[str, Any]:
    candidate = Path(raw)
    if candidate.exists() and candidate.is_file():
        text = candidate.read_text(encoding="utf-8")
    else:
        text = raw

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = yaml.safe_load(text)

    if not isinstance(payload, dict):
        raise ValidationError("--params must resolve to a JSON/YAML mapping object")
    return payload


def _coerce_typed_value(name: str, value: Any, type_name: str) -> Any:
    if type_name == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{name}: expected int, got {type(value).__name__}")
        return value

    if type_name == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{name}: expected float, got {type(value).__name__}")
        return float(value)

    if type_name == "str":
        if not isinstance(value, str):
            raise ValidationError(f"{name}: expected str, got {type(value).__name__}")
        return value

    if type_name == "bool":
        if not isinstance(value, bool):
            raise ValidationError(f"{name}: expected bool, got {type(value).__name__}")
        return value

    raise ValidationError(f"{name}: unsupported type '{type_name}' in spec")


def _validate_bounds(name: str, value: Any, bounds: dict[str, Any]) -> None:
    if not bounds:
        return

    if "min" in bounds and value < bounds["min"]:
        raise ValidationError(f"{name}: value {value} < min {bounds['min']}")
    if "max" in bounds and value > bounds["max"]:
        raise ValidationError(f"{name}: value {value} > max {bounds['max']}")

    if "step" in bounds and "min" in bounds:
        step = bounds["step"]
        base = bounds["min"]
        if isinstance(value, int):
            if step <= 0:
                raise ValidationError(f"{name}: invalid non-positive step {step}")
            if (value - base) % step != 0:
                raise ValidationError(
                    f"{name}: value {value} does not match step={step} from min={base}"
                )
        else:
            # Float step checks are tolerance-based.
            if step <= 0:
                raise ValidationError(f"{name}: invalid non-positive step {step}")
            ratio = (value - base) / step
            if abs(ratio - round(ratio)) > 1e-9:
                raise ValidationError(
                    f"{name}: value {value} does not match step={step} from min={base}"
                )


def _eval_constraint(expr: str, context: dict[str, Any]) -> bool:
    tree = ast.parse(expr, mode="eval")

    allowed_nodes = (
        ast.Expression,
        ast.BoolOp,
        ast.BinOp,
        ast.UnaryOp,
        ast.Compare,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.Call,
        ast.And,
        ast.Or,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
        ast.Pow,
        ast.USub,
        ast.UAdd,
        ast.Not,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
    )

    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValidationError(f"unsupported syntax in constraint: {expr!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id != "abs":
                raise ValidationError(f"only abs(...) is allowed in constraints: {expr!r}")
            if len(node.args) != 1:
                raise ValidationError(f"abs() must have exactly one argument: {expr!r}")
        if isinstance(node, ast.Name) and node.id not in context and node.id != "abs":
            raise ValidationError(f"unknown name '{node.id}' in constraint: {expr!r}")

    try:
        result = eval(compile(tree, "<constraint>", "eval"), {"__builtins__": {}}, {"abs": abs, **context})
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(f"failed to evaluate constraint {expr!r}: {exc}") from exc

    if not isinstance(result, bool):
        raise ValidationError(f"constraint did not evaluate to bool: {expr!r}")
    return result


def validate_params(spec: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    if "parameters" not in spec or not isinstance(spec["parameters"], list):
        raise ValidationError("spec must contain a 'parameters' list")

    param_specs = spec["parameters"]
    names: list[str] = []
    for p in param_specs:
        if not isinstance(p, dict):
            raise ValidationError("each spec parameter must be a mapping")
        name = p.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("each spec parameter must include non-empty 'name'")
        names.append(name)

    missing = sorted(set(names) - set(params))
    if missing:
        raise ValidationError(f"missing required params: {', '.join(missing)}")

    extra = sorted(set(params) - set(names))
    if extra:
        raise ValidationError(f"unexpected params not in spec: {', '.join(extra)}")

    normalized: dict[str, Any] = {}
    for p in param_specs:
        name = p["name"]
        type_name = p.get("type")
        if not isinstance(type_name, str):
            raise ValidationError(f"{name}: missing/invalid 'type' in spec")

        bounds = p.get("bounds")
        if bounds is None:
            bounds = {}
        if not isinstance(bounds, dict):
            raise ValidationError(f"{name}: 'bounds' must be a mapping")

        coerced = _coerce_typed_value(name, params[name], type_name)
        _validate_bounds(name, coerced, bounds)
        normalized[name] = coerced

    fixed_params = {}
    global_safety = spec.get("global_safety")
    if isinstance(global_safety, dict):
        fixed = global_safety.get("fixed_params", {})
        if fixed is not None:
            if not isinstance(fixed, dict):
                raise ValidationError("global_safety.fixed_params must be a mapping")
            fixed_params = dict(fixed)

        constraints = global_safety.get("constraints", [])
        if constraints is not None:
            if not isinstance(constraints, list):
                raise ValidationError("global_safety.constraints must be a list")
            context = {**fixed_params, **normalized}
            for idx, c in enumerate(constraints):
                if not isinstance(c, dict):
                    raise ValidationError(f"constraint #{idx} must be a mapping")
                expr = c.get("expr")
                name = c.get("name", f"constraint_{idx}")
                if not isinstance(expr, str) or not expr.strip():
                    raise ValidationError(f"{name}: missing non-empty expr")
                ok = _eval_constraint(expr, context)
                if not ok:
                    reason = c.get("reason")
                    detail = f" ({reason})" if isinstance(reason, str) and reason else ""
                    raise ValidationError(f"constraint failed: {name}: {expr}{detail}")

    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate params against agent param-space spec.")
    parser.add_argument("--spec", type=Path, required=True, help="Spec YAML path.")
    parser.add_argument(
        "--params",
        type=str,
        required=True,
        help="JSON/YAML mapping string or a path to a JSON/YAML file.",
    )
    args = parser.parse_args()

    try:
        spec = _load_yaml_file(args.spec)
        params = _parse_params_arg(args.params)
        normalized = validate_params(spec=spec, params=params)
    except ValidationError as exc:
        print(f"VALIDATION_ERROR: {exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"VALIDATION_ERROR: {exc}")
        return 1

    print(json.dumps(normalized, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
