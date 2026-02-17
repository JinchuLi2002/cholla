"""M0 wrapper; no behavior change; do not add new logic."""

from typing import Any

from agent.controller.controller import HybridController
from agent.controller.run_phase3c import ControllerConfig


def budget_termination(
    *,
    controller: HybridController,
    tool_calls_used: int,
    start_time: float,
) -> str:
    """Pass-through wrapper for budget termination checks."""

    return controller._budget_termination(tool_calls_used=tool_calls_used, start_time=start_time)


def budget_snapshot(
    *,
    controller: HybridController,
    iterations_completed: int,
    tool_calls_used: int,
    start_time: float,
) -> dict[str, Any]:
    """Pass-through wrapper for budget state snapshots."""

    return controller._budget_snapshot(
        iterations_completed=iterations_completed,
        tool_calls_used=tool_calls_used,
        start_time=start_time,
    )


__all__ = ["ControllerConfig", "budget_termination", "budget_snapshot"]
