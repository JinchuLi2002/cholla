from __future__ import annotations

"""Run-manifest reader helpers with canonical-first provenance semantics.

New readers should consume canonical source keys and only fall back to legacy
aliases when canonical keys are absent. New writers should not emit legacy
aliases.
"""

import json
from pathlib import Path
from typing import Any, Mapping


def _as_nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def load_run_manifest(path: str | Path) -> dict[str, Any]:
    """Load a run-manifest JSON object from disk."""
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as infile:
        raw = json.load(infile)
    if not isinstance(raw, dict):
        raise TypeError("run manifest root must be a JSON object")
    return raw


def get_source_input_paths(manifest: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """
    Resolve original input source paths from manifest content.

    Canonical keys are preferred:
    - source_params_path
    - source_schedule_path

    Deprecated fallback aliases are only used when canonical values are absent:
    - params_source_path
    - schedule_source_path
    """
    source_params = _as_nonempty_str(manifest.get("source_params_path"))
    source_schedule = _as_nonempty_str(manifest.get("source_schedule_path"))

    if source_params is None:
        source_params = _as_nonempty_str(manifest.get("params_source_path"))
    if source_schedule is None:
        source_schedule = _as_nonempty_str(manifest.get("schedule_source_path"))

    return source_params, source_schedule
