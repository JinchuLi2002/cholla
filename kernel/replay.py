"""M0 wrapper; no behavior change; do not add new logic."""

from typing import Any

from agent.controller.controller import HybridController
from agent.tools.replay_check import replay_check


def replay_controller(
    *,
    controller: HybridController,
    experiment_id: str,
    replay_mode: str = "strict",
) -> dict[str, Any]:
    """Pass-through wrapper for controller replay execution."""

    return controller.replay(experiment_id=experiment_id, replay_mode=replay_mode)


__all__ = ["replay_check", "replay_controller"]
