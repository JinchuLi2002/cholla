#!/usr/bin/env python3
"""Authoritative Phase 3C/3D runner for acceptance and replay-only execution."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Mapping

import yaml

from agent.controller.controller import HybridController
from agent.tools.registry import tool_registry_for_backend


CONTROLLER_VERSION = "phase3d_v1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_path(raw_path: Path, *, repo_root: Path) -> Path:
    if raw_path.is_absolute():
        return raw_path.resolve()
    return (repo_root / raw_path).resolve()


def _as_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _as_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _as_int(value: Any, *, label: str, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return int(value)


def _as_optional_int(value: Any, *, label: str, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return _as_int(value, label=label, minimum=minimum)


def _as_float(value: Any, *, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    parsed = float(value)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return parsed


def _as_optional_float(value: Any, *, label: str, minimum: float | None = None) -> float | None:
    if value is None:
        return None
    return _as_float(value, label=label, minimum=minimum)


def _as_bool(value: Any, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be a boolean")


@dataclass(frozen=True)
class LMConfig:
    enabled: bool
    provider: str
    model: str
    temperature: float
    max_lm_attempts: int
    seed: int | None
    lookback_k: int
    flat_tol: float
    azure_env_var_names: dict[str, str]
    openai_env_var_names: dict[str, str]


@dataclass(frozen=True)
class ControllerConfig:
    seed: int
    max_iterations: int
    max_tool_calls: int
    max_failures: int
    walltime_budget_sec: float | None
    min_success_iters: int


@dataclass(frozen=True)
class Phase3CConfig:
    lm: LMConfig
    controller: ControllerConfig
    param_space_path: str
    out_root: str
    tool_backend: str
    objective: str
    history_path: str
    experiment_id: str
    controller_run_id: str
    initial_state: dict[str, Any]


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"expected mapping YAML at {path}")
    return dict(payload)


def load_phase3c_config(config_path: Path) -> Phase3CConfig:
    payload = _load_yaml(config_path)

    lm_payload = _as_mapping(payload.get("lm", {}), label="lm")
    controller_payload = _as_mapping(payload.get("controller", {}), label="controller")
    budgets_payload = (
        _as_mapping(controller_payload.get("budgets"), label="controller.budgets")
        if "budgets" in controller_payload
        else {}
    )
    convergence_payload = (
        _as_mapping(controller_payload.get("convergence"), label="controller.convergence")
        if "convergence" in controller_payload
        else {}
    )

    lm_config = LMConfig(
        enabled=_as_bool(lm_payload.get("enabled", True), label="lm.enabled"),
        provider=str(lm_payload.get("provider", "openai")).strip().lower() or "openai",
        model=_as_string(lm_payload.get("model", "gpt-4o-mini"), label="lm.model"),
        temperature=_as_float(lm_payload.get("temperature", 1.0), label="lm.temperature"),
        max_lm_attempts=_as_int(
            lm_payload.get("max_lm_attempts", 5),
            label="lm.max_lm_attempts",
            minimum=1,
        ),
        seed=_as_optional_int(lm_payload.get("seed"), label="lm.seed", minimum=0),
        lookback_k=_as_int(
            convergence_payload.get("lookback_k", lm_payload.get("lookback_k", 5)),
            label="controller.convergence.lookback_k",
            minimum=1,
        ),
        flat_tol=_as_float(
            convergence_payload.get("flat_tol", lm_payload.get("flat_tol", 1e-6)),
            label="controller.convergence.flat_tol",
            minimum=0.0,
        ),
        azure_env_var_names=(
            _as_mapping(lm_payload.get("azure_env_var_names"), label="lm.azure_env_var_names")
            if "azure_env_var_names" in lm_payload
            else {}
        ),
        openai_env_var_names=(
            _as_mapping(lm_payload.get("openai_env_var_names"), label="lm.openai_env_var_names")
            if "openai_env_var_names" in lm_payload
            else {}
        ),
    )

    max_tool_calls_raw = controller_payload.get("max_tool_calls", budgets_payload.get("max_tool_calls", 12))
    walltime_budget_raw = controller_payload.get(
        "walltime_budget_sec",
        budgets_payload.get("walltime_budget_sec"),
    )
    history_path_raw = payload.get("history_path", "agent/history/history.jsonl")
    if not isinstance(history_path_raw, str) or not history_path_raw.strip():
        raise ValueError("history_path must be a non-empty string")

    phase_config = Phase3CConfig(
        lm=lm_config,
        controller=ControllerConfig(
            seed=_as_int(controller_payload.get("seed", 0), label="controller.seed", minimum=0),
            max_iterations=_as_int(
                controller_payload.get("max_iterations", 3),
                label="controller.max_iterations",
                minimum=1,
            ),
            max_tool_calls=_as_int(max_tool_calls_raw, label="controller.max_tool_calls", minimum=1),
            max_failures=_as_int(
                controller_payload.get("max_failures", 1),
                label="controller.max_failures",
                minimum=1,
            ),
            walltime_budget_sec=_as_optional_float(
                walltime_budget_raw,
                label="controller.walltime_budget_sec",
                minimum=0.0,
            ),
            min_success_iters=_as_int(
                controller_payload.get("min_success_iters", 3),
                label="controller.min_success_iters",
                minimum=1,
            ),
        ),
        param_space_path=_as_string(
            payload.get("param_space_path", "agent/spec/param_space_v0.yaml"),
            label="param_space_path",
        ),
        out_root=_as_string(payload.get("out_root", "r"), label="out_root"),
        tool_backend=_as_string(payload.get("tool_backend", "real"), label="tool_backend").lower(),
        objective=_as_string(payload.get("objective", "maximize_metric_scalar"), label="objective"),
        history_path=history_path_raw.strip(),
        experiment_id=str(payload.get("experiment_id", "")).strip(),
        controller_run_id=str(payload.get("controller_run_id", "")).strip(),
        initial_state=(
            _as_mapping(payload.get("initial_state"), label="initial_state")
            if "initial_state" in payload
            else {}
        ),
    )
    if phase_config.tool_backend not in {"real", "mock"}:
        raise ValueError("tool_backend must be one of: real, mock")
    return phase_config


def _sync_env_from_name(*, source_var_name: str | None, target_var_name: str) -> None:
    if not isinstance(source_var_name, str) or not source_var_name.strip():
        return
    source_value = os.environ.get(source_var_name.strip(), "").strip()
    if source_value:
        os.environ[target_var_name] = source_value


def _apply_lm_environment(lm_config: LMConfig) -> None:
    os.environ["ASTROMLAB_LM_PROVIDER"] = lm_config.provider
    if lm_config.provider == "azure":
        azure_names = lm_config.azure_env_var_names
        _sync_env_from_name(
            source_var_name=str(azure_names.get("api_key", "AZURE_OPENAI_API_KEY")),
            target_var_name="ASTROMLAB_API_KEY",
        )
        _sync_env_from_name(
            source_var_name=str(azure_names.get("endpoint", "AZURE_OPENAI_ENDPOINT")),
            target_var_name="ASTROMLAB_ENDPOINT",
        )
        _sync_env_from_name(
            source_var_name=str(azure_names.get("api_version", "AZURE_OPENAI_API_VERSION")),
            target_var_name="ASTROMLAB_API_VERSION",
        )
    elif lm_config.provider == "openai":
        openai_names = lm_config.openai_env_var_names
        _sync_env_from_name(
            source_var_name=str(openai_names.get("api_key", "OPENAI_API_KEY")),
            target_var_name="ASTROMLAB_API_KEY",
        )
        _sync_env_from_name(
            source_var_name=str(openai_names.get("base_url", "OPENAI_BASE_URL")),
            target_var_name="OPENAI_BASE_URL",
        )


def _git_head_sha(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        value = proc.stdout.strip()
        if value:
            return value
    return "unknown"


def _cli_invocation(argv: list[str]) -> str:
    args = " ".join(shlex.quote(item) for item in argv[1:])
    return f"python -m agent.controller.run_phase3c {args}".rstrip()


def _json_print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), sort_keys=True))


def _build_controller_for_run(
    *,
    repo_root: Path,
    config: Phase3CConfig,
    acceptance_replay_strict: bool,
    config_path: Path,
) -> HybridController:
    tool_registry = tool_registry_for_backend(config.tool_backend)

    if config.lm.enabled:
        _apply_lm_environment(config.lm)
        from agent.controller.agents.planner_lm import PlannerLMAgent
        from agent.controller.agents.summarizer_lm import SummarizerLMAgent

        planner = PlannerLMAgent(
            model=config.lm.model,
            temperature=config.lm.temperature,
            max_attempts=config.lm.max_lm_attempts,
            seed=config.lm.seed,
            objective=config.objective,
        )
        summarizer = SummarizerLMAgent(
            model=config.lm.model,
            temperature=config.lm.temperature,
            max_attempts=config.lm.max_lm_attempts,
            seed=config.lm.seed,
            lookback_k=config.lm.lookback_k,
            flat_tol=config.lm.flat_tol,
        )
        require_live_lm = True
    else:
        from agent.controller.agents.planner import MockPlannerAgent
        from agent.controller.agents.summarizer import MockSummarizerAgent

        planner = MockPlannerAgent(seed=config.controller.seed)
        summarizer = MockSummarizerAgent(
            lookback_k=config.lm.lookback_k,
            flat_tol=config.lm.flat_tol,
        )
        require_live_lm = False

    effective_config = {
        "config_path": str(config_path),
        "config": asdict(config),
        "acceptance_replay_strict": bool(acceptance_replay_strict),
        "controller_version": CONTROLLER_VERSION,
    }
    bundle_metadata = {
        "head_sha": _git_head_sha(repo_root),
        "controller_version": CONTROLLER_VERSION,
        "acceptance_replay_strict": bool(acceptance_replay_strict),
        "cli_invocation": _cli_invocation(sys.argv),
    }

    return HybridController(
        repo_root=repo_root,
        summarizer=summarizer,
        planner=planner,
        param_space_path=config.param_space_path,
        history_path=config.history_path,
        run_root=config.out_root,
        tool_registry=tool_registry,
        controller_run_id=config.controller_run_id,
        experiment_id=config.experiment_id,
        max_iterations=config.controller.max_iterations,
        max_tool_calls=config.controller.max_tool_calls,
        walltime_budget_sec=config.controller.walltime_budget_sec,
        max_failures=config.controller.max_failures,
        require_live_lm=require_live_lm,
        tool_backend=config.tool_backend,
        bundle_metadata=bundle_metadata,
        effective_config=effective_config,
    )


class _ReplayOnlyAgentGuard:
    model = "replay-only-guard"
    temperature = 0.0
    max_attempts = 0

    def summarize(self, _payload: Mapping[str, Any]) -> dict[str, Any]:
        raise RuntimeError("LM invocation attempted during --replay_only mode")

    def plan(self, _payload: Mapping[str, Any]) -> dict[str, Any]:
        raise RuntimeError("LM invocation attempted during --replay_only mode")


def _resolve_controller_bundle(bundle_path_raw: Path, *, repo_root: Path) -> Path:
    bundle_path = _resolve_path(bundle_path_raw, repo_root=repo_root)
    if (bundle_path / "config.json").is_file() and (bundle_path / "history_controller.jsonl").is_file():
        return bundle_path
    controller_candidate = bundle_path / "controller"
    if (controller_candidate / "config.json").is_file() and (
        controller_candidate / "history_controller.jsonl"
    ).is_file():
        return controller_candidate
    raise FileNotFoundError(f"unable to locate controller bundle at: {bundle_path_raw}")


def _tool_backend_from_bundle(bundle_dir: Path) -> str:
    config_path = bundle_dir / "config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid bundle config at {config_path}")
    tool_backend = str(payload.get("tool_backend", "")).strip().lower()
    if tool_backend in {"real", "mock"}:
        return tool_backend
    return "real"


def _experiment_id_from_bundle(bundle_dir: Path) -> str:
    payload = json.loads((bundle_dir / "config.json").read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid bundle config at {bundle_dir / 'config.json'}")
    experiment_id = payload.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ValueError(f"missing experiment_id in bundle config: {bundle_dir / 'config.json'}")
    return experiment_id.strip()


def _run_acceptance_mode(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    config_path = _resolve_path(Path(args.config), repo_root=repo_root)
    config = load_phase3c_config(config_path)

    controller = _build_controller_for_run(
        repo_root=repo_root,
        config=config,
        acceptance_replay_strict=bool(args.acceptance_replay_strict),
        config_path=config_path,
    )
    result = controller.run(initial_state=config.initial_state)
    successful_iterations = int(result.get("successful_iterations", 0) or 0)
    minimum_successes = config.controller.min_success_iters
    if successful_iterations < minimum_successes:
        raise RuntimeError(
            "acceptance criteria failed: "
            f"successful_iterations={successful_iterations} < min_success_iters={minimum_successes}"
        )

    if args.acceptance_replay_strict:
        strict_replay = controller.replay(
            experiment_id=str(result.get("experiment_id", "")),
            replay_mode="strict",
        )
        result["strict_replay"] = strict_replay
        if strict_replay.get("status") != "ok":
            raise RuntimeError(f"strict replay failed: {strict_replay}")

    result["acceptance_replay_strict"] = bool(args.acceptance_replay_strict)
    result["min_success_iters"] = minimum_successes
    _json_print(result)
    print("PHASE3D_PASS")
    return 0


def _run_replay_only_mode(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    bundle_dir = _resolve_controller_bundle(Path(args.replay_only), repo_root=repo_root)
    experiment_id = _experiment_id_from_bundle(bundle_dir)
    canonical_bundle_dir = repo_root / "agent" / "experiments" / experiment_id / "controller"
    if canonical_bundle_dir.resolve() != bundle_dir.resolve():
        raise ValueError(
            "--replay_only expects a canonical bundle under "
            f"{canonical_bundle_dir}, got {bundle_dir}"
        )

    tool_backend = _tool_backend_from_bundle(bundle_dir)
    tool_registry = tool_registry_for_backend(tool_backend)
    guard = _ReplayOnlyAgentGuard()
    controller = HybridController(
        repo_root=repo_root,
        summarizer=guard,
        planner=guard,
        tool_registry=tool_registry,
        require_live_lm=False,
        tool_backend=tool_backend,
    )
    replay_result = controller.replay(experiment_id=experiment_id, replay_mode="strict")
    if replay_result.get("status") != "ok":
        raise RuntimeError(f"replay_only mismatch: {replay_result}")
    replay_result["replay_only"] = True
    replay_result["bundle_path"] = str(bundle_dir)
    _json_print(replay_result)
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 3D acceptance/replay CLI runner.")
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="Path to Phase 3C runtime YAML config.",
    )
    parser.add_argument(
        "--acceptance_replay_strict",
        action="store_true",
        help="Run strict replay after acceptance live run and require replay status=ok.",
    )
    parser.add_argument(
        "--replay_only",
        type=str,
        default="",
        help="Replay-only mode from an existing controller bundle path; LM invocation is forbidden.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    has_config = isinstance(args.config, str) and bool(args.config.strip())
    has_replay_only = isinstance(args.replay_only, str) and bool(args.replay_only.strip())
    if has_config == has_replay_only:
        parser.error("provide exactly one of --config or --replay_only")
    if has_replay_only and args.acceptance_replay_strict:
        parser.error("--acceptance_replay_strict cannot be used with --replay_only")

    try:
        if has_replay_only:
            return _run_replay_only_mode(args)
        return _run_acceptance_mode(args)
    except Exception as exc:  # noqa: BLE001
        error_payload = {
            "status": "failed",
            "error": str(exc),
            "mode": "replay_only" if has_replay_only else "acceptance",
        }
        _json_print(error_payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
