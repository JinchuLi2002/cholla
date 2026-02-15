"""MCP-style replay integrity check wrapper for experiment bundles."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


ALLOWED_INPUT_KEYS = {"experiment_id"}
REPLAY_OK_RE = re.compile(
    r"REPLAY_OK\s+.*iterations_total=(\d+)\s+params_checked=(\d+)\s+run_id_checked=(\d+)"
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_result() -> dict[str, Any]:
    return {
        "status": "mismatch",
        "iterations_checked": 0,
        "params_checked": 0,
        "run_ids_checked": 0,
        "error": None,
    }


def _parse_payload(payload: dict[str, Any]) -> str:
    unknown_keys = sorted(set(payload.keys()) - ALLOWED_INPUT_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown payload keys: {unknown_keys}")

    raw = payload.get("experiment_id")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("experiment_id must be a non-empty string")
    return raw.strip()


def _load_run_agent_module(repo_root: Path) -> ModuleType:
    agent_dir = repo_root / "agent"
    run_agent_path = agent_dir / "run_agent.py"
    if not run_agent_path.exists():
        raise FileNotFoundError(f"missing run_agent module: {run_agent_path}")

    # run_agent.py uses sibling absolute imports (e.g. `from execute_run import ...`).
    # Insert the agent directory so in-process import resolution matches CLI behavior.
    agent_dir_str = str(agent_dir)
    if agent_dir_str not in sys.path:
        sys.path.insert(0, agent_dir_str)

    spec = importlib.util.spec_from_file_location("cholla_agent_run_agent", run_agent_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to build import spec for {run_agent_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_replay_ok(stdout_text: str) -> tuple[int, int, int] | None:
    for line in reversed(stdout_text.splitlines()):
        match = REPLAY_OK_RE.search(line.strip())
        if match:
            return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def replay_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate replay invariants for one experiment bundle."""

    result = _default_result()

    try:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        experiment_id = _parse_payload(payload)
        repo_root = _repo_root()
        run_agent_module = _load_run_agent_module(repo_root)
        replay_fn = getattr(run_agent_module, "_run_replay_mode", None)
        if replay_fn is None:
            raise AttributeError("run_agent._run_replay_mode is unavailable")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exit_code = int(replay_fn(repo_root=repo_root, experiment_id=experiment_id))

        stdout_text = stdout_buf.getvalue()
        stderr_text = stderr_buf.getvalue()
        parsed = _parse_replay_ok(stdout_text)

        if exit_code == 0 and parsed is not None:
            iterations_checked, params_checked, run_ids_checked = parsed
            result.update(
                {
                    "status": "ok",
                    "iterations_checked": iterations_checked,
                    "params_checked": params_checked,
                    "run_ids_checked": run_ids_checked,
                    "error": None,
                }
            )
            return result

        error_text = (stderr_text.strip() or stdout_text.strip() or "replay check failed")
        result["error"] = error_text
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result


__all__ = ["replay_check"]

