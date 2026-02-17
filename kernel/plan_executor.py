"""M0 wrapper; no behavior change; do not add new logic."""

import importlib
from pathlib import Path
import sys
from typing import Any


def _load_execute_run_module():
    agent_dir = Path(__file__).resolve().parents[1] / "agent"
    agent_dir_str = str(agent_dir)
    if agent_dir_str not in sys.path:
        sys.path.insert(0, agent_dir_str)
    return importlib.import_module("agent.execute_run")


def render_params(template_text: str, overrides: dict[str, Any]) -> str:
    """Pass-through wrapper for params rendering."""

    module = _load_execute_run_module()
    return module._render_params(template_text, overrides)


def execute_run(
    *,
    repo_root: Path,
    seed: int,
    iteration: int,
    agent_run_id: str,
    spec_path: Path,
    template_params_path: Path,
    template_schedule_path: Path,
) -> dict[str, Any]:
    """Pass-through wrapper for one iteration execution."""

    module = _load_execute_run_module()
    return module.execute_run(
        repo_root=repo_root,
        seed=seed,
        iteration=iteration,
        agent_run_id=agent_run_id,
        spec_path=spec_path,
        template_params_path=template_params_path,
        template_schedule_path=template_schedule_path,
    )


def execute_plan(
    *,
    repo_root: Path,
    seed: int,
    iteration: int,
    agent_run_id: str,
    spec_path: Path,
    template_params_path: Path,
    template_schedule_path: Path,
) -> dict[str, Any]:
    """Pass-through wrapper for one iteration execution."""

    return execute_run(
        repo_root=repo_root,
        seed=seed,
        iteration=iteration,
        agent_run_id=agent_run_id,
        spec_path=spec_path,
        template_params_path=template_params_path,
        template_schedule_path=template_schedule_path,
    )


__all__ = ["execute_run", "execute_plan", "render_params"]
