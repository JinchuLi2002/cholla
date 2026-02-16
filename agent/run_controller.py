#!/usr/bin/env python3
"""CLI entrypoint for HybridController replay modes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _repo_root_from_file() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_repo_root_on_path() -> Path:
    repo_root = _repo_root_from_file()
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return repo_root


def _build_controller(*, repo_root: Path, replay_mode: str):
    from agent.controller.agents import (
        MockPlannerAgent,
        MockSummarizerAgent,
        PlannerAgent,
        SummarizerAgent,
    )
    from agent.controller.controller import HybridController

    mode = replay_mode.strip().lower()
    if mode == "live":
        planner = PlannerAgent()
        summarizer = SummarizerAgent()
    else:
        planner = MockPlannerAgent()
        summarizer = MockSummarizerAgent()

    return HybridController(
        repo_root=repo_root,
        summarizer=summarizer,
        planner=planner,
    )


def _failure_result(*, experiment_id: str, replay_mode: str, error: str) -> dict[str, Any]:
    return {
        "status": "mismatch",
        "experiment_id": experiment_id,
        "replay_mode": replay_mode,
        "iterations_checked": 0,
        "params_checked": 0,
        "run_ids_checked": 0,
        "metrics_checked": 0,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run HybridController replay in strict or live mode.")
    parser.add_argument(
        "--replay",
        type=str,
        required=True,
        help="Controller experiment id under agent/experiments/<id>/controller.",
    )
    parser.add_argument(
        "--replay_mode",
        type=str,
        default="strict",
        choices=["strict", "live"],
        help="Replay mode: strict (stored specs + tool re-exec checks) or live (strict + LM diffs).",
    )
    parser.add_argument(
        "--repo_root",
        type=Path,
        default=None,
        help="Optional repository root override. Defaults to parent of this script.",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve() if args.repo_root is not None else _ensure_repo_root_on_path()
    if args.repo_root is not None:
        repo_root_str = str(repo_root)
        if repo_root_str not in sys.path:
            sys.path.insert(0, repo_root_str)

    experiment_id = args.replay.strip()
    replay_mode = args.replay_mode.strip().lower()

    try:
        controller = _build_controller(repo_root=repo_root, replay_mode=replay_mode)
        result = controller.replay(experiment_id=experiment_id, replay_mode=replay_mode)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        error_result = _failure_result(
            experiment_id=experiment_id,
            replay_mode=replay_mode,
            error=str(exc),
        )
        print(json.dumps(error_result, sort_keys=True))
        print(f"RUN_CONTROLLER_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
