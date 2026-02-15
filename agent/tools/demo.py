"""Minimal tool-runner harness for MCP-style Tier-4 tools."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from agent.tools.registry import TOOLS


def _resolve_callable(callable_ref: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if callable(callable_ref):
        return callable_ref

    if not isinstance(callable_ref, str) or ":" not in callable_ref:
        raise TypeError(f"invalid callable reference: {callable_ref!r}")

    module_name, attr_name = callable_ref.split(":", 1)
    if not module_name or not attr_name:
        raise TypeError(f"invalid callable reference: {callable_ref!r}")

    module = importlib.import_module(module_name)
    fn = getattr(module, attr_name, None)
    if not callable(fn):
        raise TypeError(f"resolved attribute is not callable: {callable_ref!r}")
    return fn


def list_tools() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in sorted(TOOLS):
        desc = TOOLS[name]
        out.append(
            {
                "name": name,
                "description": desc.get("description", ""),
                "callable": desc.get("callable"),
            }
        )
    return out


def invoke_tool(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if name not in TOOLS:
        raise KeyError(f"unknown tool: {name}")

    descriptor = TOOLS[name]
    tool_fn = _resolve_callable(descriptor.get("callable"))
    result = tool_fn(payload)
    if not isinstance(result, dict):
        raise TypeError(f"tool {name!r} returned non-object payload: {type(result).__name__}")
    return result


def _load_payload(payload_text: str | None, payload_file: str | None) -> dict[str, Any]:
    if payload_text is not None and payload_file is not None:
        raise ValueError("use only one of --payload or --payload-file")

    text = "{}"
    if payload_text is not None:
        text = payload_text
    elif payload_file is not None:
        if payload_file == "-":
            text = sys.stdin.read()
        else:
            text = Path(payload_file).read_text(encoding="utf-8")

    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("payload must decode to a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Tier-4 tool by name with JSON payload.")
    parser.add_argument("--tool", type=str, default="", help="Tool name from agent.tools.registry.TOOLS.")
    parser.add_argument("--payload", type=str, default=None, help="Inline JSON payload object string.")
    parser.add_argument("--payload-file", type=str, default=None, help="Path to JSON payload file, or '-' for stdin.")
    parser.add_argument("--list-tools", action="store_true", help="List available tools and exit.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    try:
        if args.list_tools:
            response: dict[str, Any] = {"tools": list_tools()}
        else:
            if not args.tool:
                raise ValueError("--tool is required unless --list-tools is used")
            payload = _load_payload(args.payload, args.payload_file)
            response = invoke_tool(args.tool, payload)
    except Exception as exc:  # noqa: BLE001
        response = {"status": "error", "error": str(exc)}
        print(json.dumps(response, indent=2 if args.pretty else None, sort_keys=True))
        return 1

    print(json.dumps(response, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

