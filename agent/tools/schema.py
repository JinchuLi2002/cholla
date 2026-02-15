"""Draft-07 style JSON schema dictionaries for Tier-4 MCP-style tools."""

from __future__ import annotations


RUN_CHOLLA_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "run_cholla_input",
    "type": "object",
    "properties": {
        "params_text": {"type": "string", "description": "Full params template text to run."},
        "schedule_text": {"type": "string", "description": "Full scale_outputs text to run."},
        "out_root": {
            "type": ["string", "null"],
            "description": "Optional backend output root directory.",
            "default": None,
        },
        "run_id": {
            "type": ["string", "null"],
            "description": "Optional explicit RUN_ID override.",
            "default": None,
        },
    },
    "required": ["params_text", "schedule_text"],
    "additionalProperties": False,
}


RUN_CHOLLA_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "run_cholla_output",
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "run_id": {"type": "string"},
        "run_dir": {"type": "string"},
        "run_manifest_path": {"type": "string"},
        "validation_path": {"type": "string"},
        "produced_files": {"type": "array", "items": {"type": "string"}},
        "error": {"type": "string"},
    },
    "required": [
        "status",
        "run_id",
        "run_dir",
        "run_manifest_path",
        "validation_path",
        "produced_files",
        "error",
    ],
    "additionalProperties": True,
}


COMPUTE_METRIC_V3_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "compute_metric_v3_input",
    "type": "object",
    "properties": {
        "run_manifest_path": {"type": "string", "description": "Path to run_manifest JSON."},
    },
    "required": ["run_manifest_path"],
    "additionalProperties": False,
}


COMPUTE_METRIC_V3_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "compute_metric_v3_output",
    "type": "object",
    "properties": {
        "metric_name": {"type": "string"},
        "scalar": {"type": ["number", "integer", "null"]},
        "details": {"type": "object"},
    },
    "required": [
        "metric_name",
        "scalar",
        "details",
    ],
    "additionalProperties": True,
}


REPLAY_CHECK_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "replay_check_input",
    "type": "object",
    "properties": {
        "experiment_id": {
            "type": "string",
            "description": "Experiment id under agent/experiments/<id>/.",
        }
    },
    "required": ["experiment_id"],
    "additionalProperties": False,
}


REPLAY_CHECK_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "replay_check_output",
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "mismatch"]},
        "iterations_checked": {"type": "integer"},
        "params_checked": {"type": "integer"},
        "run_ids_checked": {"type": "integer"},
        "error": {"type": ["string", "null"]},
    },
    "required": ["status", "iterations_checked", "params_checked", "run_ids_checked", "error"],
    "additionalProperties": False,
}


# Backward-compatible aliases.
REPLAY_VERIFY_INPUT_SCHEMA = REPLAY_CHECK_INPUT_SCHEMA
REPLAY_VERIFY_OUTPUT_SCHEMA = REPLAY_CHECK_OUTPUT_SCHEMA


VALIDATE_PARAMS_INPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "validate_params_input",
    "type": "object",
    "properties": {
        "params": {"type": "object"},
    },
    "required": ["params"],
    "additionalProperties": False,
}


VALIDATE_PARAMS_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "validate_params_output",
    "type": "object",
    "properties": {
        "valid": {"type": "boolean"},
        "errors": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["valid", "errors"],
    "additionalProperties": False,
}


ALL_SCHEMAS = {
    "run_cholla_input": RUN_CHOLLA_INPUT_SCHEMA,
    "run_cholla_output": RUN_CHOLLA_OUTPUT_SCHEMA,
    "compute_metric_v3_input": COMPUTE_METRIC_V3_INPUT_SCHEMA,
    "compute_metric_v3_output": COMPUTE_METRIC_V3_OUTPUT_SCHEMA,
    "replay_check_input": REPLAY_CHECK_INPUT_SCHEMA,
    "replay_check_output": REPLAY_CHECK_OUTPUT_SCHEMA,
    "replay_verify_input": REPLAY_VERIFY_INPUT_SCHEMA,
    "replay_verify_output": REPLAY_VERIFY_OUTPUT_SCHEMA,
    "validate_params_input": VALIDATE_PARAMS_INPUT_SCHEMA,
    "validate_params_output": VALIDATE_PARAMS_OUTPUT_SCHEMA,
}
