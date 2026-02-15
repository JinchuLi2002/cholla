"""Tool registry skeleton for Tier-4 MCP-style invocation."""

from __future__ import annotations

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


TOOLS = {
    "run_cholla": {
        "name": "run_cholla",
        "description": "Run one backend Cholla smoke execution from in-memory params/schedule text.",
        "input_schema": RUN_CHOLLA_INPUT_SCHEMA,
        "output_schema": RUN_CHOLLA_OUTPUT_SCHEMA,
        "callable": "agent.tools.run_cholla:run_cholla",
    },
    "compute_metric": {
        "name": "compute_metric",
        "description": "Compute log-final-step metric from a run_manifest path (read-only).",
        "input_schema": COMPUTE_METRIC_V3_INPUT_SCHEMA,
        "output_schema": COMPUTE_METRIC_V3_OUTPUT_SCHEMA,
        "callable": "agent.tools.compute_metric:compute_metric",
    },
    "compute_metric_v3": {
        "name": "compute_metric_v3",
        "description": "Compute log-final-step metric from a run_manifest path (read-only).",
        "input_schema": COMPUTE_METRIC_V3_INPUT_SCHEMA,
        "output_schema": COMPUTE_METRIC_V3_OUTPUT_SCHEMA,
        "callable": "agent.tools.compute_metric:compute_metric",
    },
    "replay_check": {
        "name": "replay_check",
        "description": "Replay-integrity check for a bundled experiment id.",
        "input_schema": REPLAY_CHECK_INPUT_SCHEMA,
        "output_schema": REPLAY_CHECK_OUTPUT_SCHEMA,
        "callable": "agent.tools.replay_check:replay_check",
    },
    "replay_verify": {
        "name": "replay_verify",
        "description": "Replay-integrity check for a bundled experiment id (compat alias).",
        "input_schema": REPLAY_CHECK_INPUT_SCHEMA,
        "output_schema": REPLAY_CHECK_OUTPUT_SCHEMA,
        "callable": "agent.tools.replay_check:replay_check",
    },
    "validate_params": {
        "name": "validate_params",
        "description": "Validate a params object against the canonical Tier-4 param-space spec.",
        "input_schema": VALIDATE_PARAMS_INPUT_SCHEMA,
        "output_schema": VALIDATE_PARAMS_OUTPUT_SCHEMA,
        "callable": "agent.tools.validate_params:validate_params",
    },
}
