"""Class-based deterministic tool registry for kernel/controller wiring."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
import importlib
import inspect
import json
from typing import Any, TypeAlias

from agent.controller.specs import SchemaValidationError, validate_json_schema
from agent.tools.registry import TOOLS as _LEGACY_TOOLS
from agent.tools.schema import (
    COMPUTE_METRIC_V3_INPUT_SCHEMA,
    COMPUTE_METRIC_V3_OUTPUT_SCHEMA,
    REPLAY_CHECK_INPUT_SCHEMA,
    REPLAY_CHECK_OUTPUT_SCHEMA,
    RUN_CHOLLA_INPUT_SCHEMA,
    RUN_CHOLLA_OUTPUT_SCHEMA,
    VALIDATE_PARAMS_INPUT_SCHEMA,
    VALIDATE_PARAMS_OUTPUT_SCHEMA,
)


ToolCallableNoContext: TypeAlias = Callable[[dict[str, Any]], Any]
ToolCallableWithContext: TypeAlias = Callable[[dict[str, Any], Mapping[str, Any]], Any]
ToolCallableRef: TypeAlias = str | ToolCallableNoContext | ToolCallableWithContext
ToolInvoker: TypeAlias = Callable[[dict[str, Any], Mapping[str, Any]], Any]

_CONTEXT_MARKER = "__tool_accepts_context__"
_MISSING = object()


@dataclass(frozen=True)
class ToolSpec:
    """Deterministic tool metadata and invocation schema contract."""

    name: str
    callable: ToolCallableRef
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None
    version: str


@dataclass(frozen=True)
class _ToolRegistration:
    """Factory-time tool registration metadata with backend callables."""

    name: str
    real_callable: str
    mock_callable: str | None
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None
    version: str


class ToolRegistryError(ValueError):
    """Raised when deterministic tool registration/invocation fails."""


class ToolRegistry(Mapping[str, dict[str, Any]]):
    """Deterministic tool registry with schema-validated invocation."""

    def __init__(self, specs: list[ToolSpec] | tuple[ToolSpec, ...] | None = None) -> None:
        self._catalog: tuple[ToolSpec, ...] = ()
        self._by_name: dict[str, ToolSpec] = {}
        self._invokers: dict[str, ToolInvoker] = {}

        if specs is not None:
            for spec in specs:
                self.register(
                    name=spec.name,
                    callable=spec.callable,
                    input_schema=spec.input_schema,
                    output_schema=spec.output_schema,
                    version=spec.version,
                )

    @property
    def catalog(self) -> tuple[ToolSpec, ...]:
        return self._catalog

    def register(
        self,
        name: str,
        callable: ToolCallableRef,
        input_schema: Mapping[str, Any],
        output_schema: Mapping[str, Any] | None,
        version: str,
    ) -> None:
        tool_name = _normalize_non_empty_str(name=name, label="name")
        tool_version = _normalize_non_empty_str(name=version, label="version")
        callable_ref = callable

        if tool_name in self._by_name:
            raise ToolRegistryError(
                _stable_error_text(
                    code="tool_already_registered",
                    phase="register",
                    detail=f"tool {tool_name!r} is already registered",
                    tool=tool_name,
                    version=tool_version,
                )
            )

        if not isinstance(input_schema, Mapping):
            raise ToolRegistryError(
                _stable_error_text(
                    code="invalid_input_schema",
                    phase="register",
                    detail="input_schema must be a mapping",
                    tool=tool_name,
                    version=tool_version,
                    payload=input_schema,
                )
            )
        if output_schema is not None and not isinstance(output_schema, Mapping):
            raise ToolRegistryError(
                _stable_error_text(
                    code="invalid_output_schema",
                    phase="register",
                    detail="output_schema must be a mapping or null",
                    tool=tool_name,
                    version=tool_version,
                    payload=output_schema,
                )
            )

        input_schema_copy = _canonicalize_mapping(input_schema)
        output_schema_copy = (
            _canonicalize_mapping(output_schema) if isinstance(output_schema, Mapping) else None
        )

        try:
            invoker = _build_invoker(callable_ref)
        except Exception as exc:  # noqa: BLE001
            raise ToolRegistryError(
                _stable_error_text(
                    code="invalid_tool_callable",
                    phase="register",
                    detail=str(exc),
                    tool=tool_name,
                    version=tool_version,
                )
            ) from exc

        spec = ToolSpec(
            name=tool_name,
            callable=callable_ref,
            input_schema=input_schema_copy,
            output_schema=output_schema_copy,
            version=tool_version,
        )

        next_by_name = dict(self._by_name)
        next_by_name[tool_name] = spec
        next_invokers = dict(self._invokers)
        next_invokers[tool_name] = invoker

        self._by_name = next_by_name
        self._invokers = next_invokers
        self._catalog = tuple(sorted(next_by_name.values(), key=lambda item: item.name))

    def invoke(
        self,
        name: str,
        args: Mapping[str, Any],
        context: Mapping[str, Any] | None,
    ) -> Any:
        tool_name = _normalize_non_empty_str(name=name, label="name")
        spec = self._by_name.get(tool_name)
        if spec is None:
            available = [item.name for item in self._catalog]
            raise ToolRegistryError(
                _stable_error_text(
                    code="unknown_tool",
                    phase="invoke",
                    detail=f"unknown tool: {tool_name!r}",
                    tool=tool_name,
                    payload={"available_tools": available},
                )
            )

        if not isinstance(args, Mapping):
            raise ToolRegistryError(
                _stable_error_text(
                    code="invalid_args_type",
                    phase="invoke_input",
                    detail="args must be a mapping",
                    tool=spec.name,
                    version=spec.version,
                    payload=args,
                )
            )
        if context is not None and not isinstance(context, Mapping):
            raise ToolRegistryError(
                _stable_error_text(
                    code="invalid_context_type",
                    phase="invoke_input",
                    detail="context must be a mapping or null",
                    tool=spec.name,
                    version=spec.version,
                    payload=context,
                )
            )

        args_payload = _canonicalize_mapping(args)
        context_payload = _canonicalize_mapping(context) if isinstance(context, Mapping) else {}
        self._validate_schema(
            payload=args_payload,
            schema=spec.input_schema,
            tool=spec,
            phase="invoke_input",
        )

        try:
            output = self._invokers[spec.name](deepcopy(args_payload), deepcopy(context_payload))
        except Exception as exc:  # noqa: BLE001
            raise ToolRegistryError(
                _stable_error_text(
                    code="tool_execution_failed",
                    phase="invoke_call",
                    detail=str(exc),
                    tool=spec.name,
                    version=spec.version,
                    payload={"args": args_payload},
                )
            ) from exc

        if spec.output_schema is not None:
            self._validate_schema(
                payload=output,
                schema=spec.output_schema,
                tool=spec,
                phase="invoke_output",
            )
        return output

    def __getitem__(self, tool_name: str) -> dict[str, Any]:
        spec = self._by_name[tool_name]
        return {
            "name": spec.name,
            "callable": spec.callable,
            "input_schema": deepcopy(spec.input_schema),
            "output_schema": deepcopy(spec.output_schema),
            "version": spec.version,
        }

    def __iter__(self) -> Iterator[str]:
        for spec in self._catalog:
            yield spec.name

    def __len__(self) -> int:
        return len(self._catalog)

    def _validate_schema(
        self,
        *,
        payload: Any,
        schema: Mapping[str, Any],
        tool: ToolSpec,
        phase: str,
    ) -> None:
        canonical_payload = _canonicalize_mapping(payload)
        try:
            validate_json_schema(
                canonical_payload,
                schema,
                schema_name=f"{tool.name}.{phase}",
            )
        except SchemaValidationError as exc:
            raise ToolRegistryError(
                _stable_error_text(
                    code="schema_validation_failed",
                    phase=phase,
                    detail=str(exc),
                    tool=tool.name,
                    version=tool.version,
                    payload=canonical_payload,
                )
            ) from exc


def _normalize_non_empty_str(*, name: Any, label: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return name.strip()


def _resolve_tool_callable(callable_ref: ToolCallableRef) -> Callable[..., Any]:
    if callable(callable_ref):
        return callable_ref

    if not isinstance(callable_ref, str) or ":" not in callable_ref:
        raise TypeError(f"invalid tool callable reference: {callable_ref!r}")

    module_name, attr_name = callable_ref.split(":", 1)
    if not module_name or not attr_name:
        raise TypeError(f"invalid tool callable reference: {callable_ref!r}")

    module = importlib.import_module(module_name)
    fn = getattr(module, attr_name, None)
    if not callable(fn):
        raise TypeError(f"resolved tool callable is not callable: {callable_ref!r}")
    return fn


def _build_invoker(callable_ref: ToolCallableRef) -> ToolInvoker:
    fn = _resolve_tool_callable(callable_ref)
    with_context = _callable_accepts_context(fn)

    if with_context:
        return lambda args, context: fn(args, context)
    return lambda args, _context: fn(args)


def _callable_accepts_context(fn: Callable[..., Any]) -> bool:
    marker = getattr(fn, _CONTEXT_MARKER, None)
    if isinstance(marker, bool):
        return marker

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise TypeError("tool callable signature is not introspectable; wrap with explicit adapter") from exc

    positional_params: list[inspect.Parameter] = []
    for param in signature.parameters.values():
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional_params.append(param)
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TypeError("tool callable using *args/**kwargs is unsupported; use explicit wrapper")
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            raise TypeError("tool callable using keyword-only parameters is unsupported")

    if len(positional_params) == 1:
        return False
    if len(positional_params) == 2:
        return True
    raise TypeError("tool callable must accept exactly one arg (args) or two args (args, context)")


def _canonicalize_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda item: _stable_sort_key(item[0]))
        return {key: _canonicalize_mapping(item_value) for key, item_value in ordered}
    if isinstance(value, list):
        return [_canonicalize_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_canonicalize_mapping(item) for item in value)
    return deepcopy(value)


def _stable_sort_key(value: Any) -> tuple[str, str]:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return (type(value).__name__, json.dumps(value, sort_keys=True))
    return (type(value).__name__, f"<non_json:{type(value).__name__}>")


def _json_safe_stable(value: Any) -> Any:
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda item: _stable_sort_key(item[0]))
        return {str(key): _json_safe_stable(item_value) for key, item_value in ordered}
    if isinstance(value, list):
        return [_json_safe_stable(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_stable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<non_json:{type(value).__name__}>"


def _stable_error_text(
    *,
    code: str,
    phase: str,
    detail: str,
    tool: str | None = None,
    version: str | None = None,
    payload: Any = _MISSING,
) -> str:
    error_payload: dict[str, Any] = {
        "code": code,
        "phase": phase,
        "detail": detail,
    }
    if tool is not None:
        error_payload["tool"] = tool
    if version is not None:
        error_payload["version"] = version
    if payload is not _MISSING:
        error_payload["payload"] = payload
    return json.dumps(
        _json_safe_stable(error_payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


_TOOL_REGISTRATIONS: tuple[_ToolRegistration, ...] = (
    _ToolRegistration(
        name="compute_metric",
        real_callable="agent.tools.compute_metric:compute_metric",
        mock_callable="agent.tools.mock_backend:compute_metric",
        input_schema=COMPUTE_METRIC_V3_INPUT_SCHEMA,
        output_schema=COMPUTE_METRIC_V3_OUTPUT_SCHEMA,
        version="v3",
    ),
    _ToolRegistration(
        name="compute_metric_v3",
        real_callable="agent.tools.compute_metric:compute_metric",
        mock_callable="agent.tools.mock_backend:compute_metric",
        input_schema=COMPUTE_METRIC_V3_INPUT_SCHEMA,
        output_schema=COMPUTE_METRIC_V3_OUTPUT_SCHEMA,
        version="v3",
    ),
    _ToolRegistration(
        name="replay_check",
        real_callable="agent.tools.replay_check:replay_check",
        mock_callable=None,
        input_schema=REPLAY_CHECK_INPUT_SCHEMA,
        output_schema=REPLAY_CHECK_OUTPUT_SCHEMA,
        version="v1",
    ),
    _ToolRegistration(
        name="replay_verify",
        real_callable="agent.tools.replay_check:replay_check",
        mock_callable=None,
        input_schema=REPLAY_CHECK_INPUT_SCHEMA,
        output_schema=REPLAY_CHECK_OUTPUT_SCHEMA,
        version="v1",
    ),
    _ToolRegistration(
        name="run_cholla",
        real_callable="agent.tools.run_cholla:run_cholla",
        mock_callable="agent.tools.mock_backend:run_cholla",
        input_schema=RUN_CHOLLA_INPUT_SCHEMA,
        output_schema=RUN_CHOLLA_OUTPUT_SCHEMA,
        version="v1",
    ),
    _ToolRegistration(
        name="validate_params",
        real_callable="agent.tools.validate_params:validate_params",
        mock_callable=None,
        input_schema=VALIDATE_PARAMS_INPUT_SCHEMA,
        output_schema=VALIDATE_PARAMS_OUTPUT_SCHEMA,
        version="v1",
    ),
)


def _build_compat_tools() -> dict[str, dict[str, Any]]:
    compat = deepcopy(_LEGACY_TOOLS)
    for spec in _TOOL_REGISTRATIONS:
        existing = compat.get(spec.name, {})
        description = existing.get("description", "")
        compat[spec.name] = {
            "name": spec.name,
            "description": description,
            "input_schema": deepcopy(spec.input_schema),
            "output_schema": deepcopy(spec.output_schema),
            "callable": spec.real_callable,
            "version": spec.version,
        }
    return compat


TOOLS: dict[str, dict[str, Any]] = _build_compat_tools()


def tool_registry_for_backend(tool_backend: str) -> ToolRegistry:
    """Return a deterministic registry configured for the selected backend."""

    backend = tool_backend.strip().lower()
    if backend not in {"real", "mock"}:
        raise ValueError(f"unknown tool backend: {tool_backend!r} (expected 'real' or 'mock')")

    registry = ToolRegistry()
    for spec in _TOOL_REGISTRATIONS:
        callable_ref = spec.real_callable
        if backend == "mock" and isinstance(spec.mock_callable, str):
            callable_ref = spec.mock_callable
        registry.register(
            name=spec.name,
            callable=callable_ref,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            version=spec.version,
        )
    return registry


__all__ = [
    "TOOLS",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolSpec",
    "tool_registry_for_backend",
]
