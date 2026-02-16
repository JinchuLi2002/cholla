#!/usr/bin/env python3
"""CLI entrypoint for HybridController live runs and replay."""

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


def _failure_result(
    *,
    mode: str,
    experiment_id: str,
    replay_mode: str | None,
    error: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "mismatch",
        "mode": mode,
        "experiment_id": experiment_id,
        "error": error,
    }
    if replay_mode is not None:
        payload["replay_mode"] = replay_mode
    return payload


def _build_replay_controller(*, repo_root: Path, replay_mode: str):
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


def _build_live_controller(*, repo_root: Path, args: argparse.Namespace):
    from agent.controller.agents import PlannerAgent, SummarizerAgent
    from agent.controller.controller import HybridController

    planner_kwargs: dict[str, Any] = {}
    summarizer_kwargs: dict[str, Any] = {}
    if args.model:
        planner_kwargs["model"] = args.model
        summarizer_kwargs["model"] = args.model
    if args.temperature is not None:
        planner_kwargs["temperature"] = float(args.temperature)
        summarizer_kwargs["temperature"] = float(args.temperature)
    if args.max_attempts is not None:
        planner_kwargs["max_attempts"] = int(args.max_attempts)
        summarizer_kwargs["max_attempts"] = int(args.max_attempts)
    if args.seed is not None:
        planner_kwargs["seed"] = int(args.seed)
        summarizer_kwargs["seed"] = int(args.seed)
    if args.lookback_k is not None:
        summarizer_kwargs["lookback_k"] = int(args.lookback_k)

    planner = PlannerAgent(**planner_kwargs)
    summarizer = SummarizerAgent(**summarizer_kwargs)

    return HybridController(
        repo_root=repo_root,
        summarizer=summarizer,
        planner=planner,
        history_path=args.history_path,
        controller_run_id=args.controller_run_id,
        experiment_id=args.experiment_id,
        max_iterations=args.max_iterations,
        max_tool_calls=args.max_tool_calls,
        walltime_budget_sec=args.walltime_budget_sec,
        max_failures=args.max_failures,
        require_live_lm=args.require_live_lm,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run HybridController live or replay modes.")
    parser.add_argument(
        "--repo_root",
        type=Path,
        default=None,
        help="Optional repository root override. Defaults to parent of this script.",
    )

    parser.add_argument(
        "--replay",
        type=str,
        default="",
        help="Optional experiment id under agent/experiments/<id>/controller to replay.",
    )
    parser.add_argument(
        "--replay_mode",
        type=str,
        default="strict",
        choices=["strict", "live"],
        help="Replay mode when --replay is provided.",
    )

    parser.add_argument(
        "--acceptance_replay_strict",
        action="store_true",
        help="After a successful live run, execute strict replay and fail on mismatch.",
    )

    parser.add_argument(
        "--experiment_id",
        type=str,
        default="",
        help="Optional experiment id for live run.",
    )
    parser.add_argument(
        "--controller_run_id",
        type=str,
        default="",
        help="Optional controller run id for live run.",
    )
    parser.add_argument(
        "--history_path",
        type=Path,
        default=Path("agent/history/history.jsonl"),
        help="History JSONL path for live run.",
    )
    parser.add_argument(
        "--max_iterations",
        type=int,
        default=3,
        help="Max live controller iterations.",
    )
    parser.add_argument(
        "--max_tool_calls",
        type=int,
        default=12,
        help="Max live tool calls.",
    )
    parser.add_argument(
        "--max_failures",
        type=int,
        default=1,
        help="Max failed iterations before stop.",
    )
    parser.add_argument(
        "--walltime_budget_sec",
        type=float,
        default=1800.0,
        help="Walltime budget in seconds for live run.",
    )
    parser.add_argument(
        "--require_live_lm",
        dest="require_live_lm",
        action="store_true",
        default=True,
        help="Require live LM traces from planner/summarizer (default true).",
    )
    parser.add_argument(
        "--allow_non_live_lm",
        dest="require_live_lm",
        action="store_false",
        help="Disable live LM requirement for local debugging.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="",
        help="Optional planner/summarizer model override for live run.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Optional planner/summarizer temperature override.",
    )
    parser.add_argument(
        "--max_attempts",
        type=int,
        default=None,
        help="Optional LM retry max attempts override.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional LM seed override.",
    )
    parser.add_argument(
        "--lookback_k",
        type=int,
        default=None,
        help="Optional summarizer lookback window override.",
    )
    parser.add_argument(
        "--initial_state_json",
        type=str,
        default="{}",
        help="Initial controller state JSON object for live run.",
    )
    return parser.parse_args()


def _parse_initial_state(raw_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid --initial_state_json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("--initial_state_json must decode to a JSON object")
    return dict(payload)


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve() if args.repo_root is not None else _ensure_repo_root_on_path()
    if args.repo_root is not None:
        repo_root_str = str(repo_root)
        if repo_root_str not in sys.path:
            sys.path.insert(0, repo_root_str)

    replay_experiment_id = args.replay.strip()
    replay_mode = args.replay_mode.strip().lower()

    try:
        if replay_experiment_id:
            controller = _build_replay_controller(repo_root=repo_root, replay_mode=replay_mode)
            result = controller.replay(experiment_id=replay_experiment_id, replay_mode=replay_mode)
            print(json.dumps(result, sort_keys=True))
            return 0

        initial_state = _parse_initial_state(args.initial_state_json)
        controller = _build_live_controller(repo_root=repo_root, args=args)
        result = controller.run(initial_state=initial_state)
        result["acceptance_replay_strict"] = bool(args.acceptance_replay_strict)

        if args.acceptance_replay_strict:
            failed_iterations = int(result.get("failed_iterations", 0) or 0)
            if failed_iterations > 0:
                print(json.dumps(result, sort_keys=True))
                return 1
            strict_replay = controller.replay(result["experiment_id"], replay_mode="strict")
            result["strict_replay"] = strict_replay
            print(json.dumps(result, sort_keys=True))
            if strict_replay.get("status") != "ok":
                return 1
            return 0

        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        mode = "replay" if replay_experiment_id else "live"
        error_result = _failure_result(
            mode=mode,
            experiment_id=replay_experiment_id or args.experiment_id.strip(),
            replay_mode=replay_mode if replay_experiment_id else None,
            error=str(exc),
        )
        print(json.dumps(error_result, sort_keys=True))
        print(f"RUN_CONTROLLER_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
