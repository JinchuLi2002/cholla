"""Shared deterministic helpers for stub tools."""

from __future__ import annotations

import hashlib
import json
from typing import Any


_ALLOWED_KEYS = {"seed", "count", "offset", "scale"}


def normalize_stub_payload(payload: dict[str, Any], *, allow_scale: bool) -> dict[str, Any]:
    """Normalize and validate deterministic stub payload fields."""

    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    unknown = sorted(set(payload.keys()) - _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"unknown payload keys: {unknown}")

    seed_raw = payload.get("seed")
    if not isinstance(seed_raw, int) or isinstance(seed_raw, bool):
        raise ValueError("seed must be an integer")
    if seed_raw < 0:
        raise ValueError("seed must be >= 0")
    seed = int(seed_raw)

    count_raw = payload.get("count", 8)
    if not isinstance(count_raw, int) or isinstance(count_raw, bool):
        raise ValueError("count must be an integer")
    if count_raw < 1:
        raise ValueError("count must be >= 1")
    if count_raw > 4096:
        raise ValueError("count must be <= 4096")
    count = int(count_raw)

    offset_raw = payload.get("offset", 0.0)
    if isinstance(offset_raw, bool) or not isinstance(offset_raw, (int, float)):
        raise ValueError("offset must be numeric")
    offset = float(offset_raw)

    normalized = {
        "seed": seed,
        "count": count,
        "offset": offset,
    }
    if allow_scale:
        scale_raw = payload.get("scale", 1.0)
        if isinstance(scale_raw, bool) or not isinstance(scale_raw, (int, float)):
            raise ValueError("scale must be numeric")
        normalized["scale"] = float(scale_raw)

    return normalized


def deterministic_series(*, seed: int, count: int, offset: float) -> list[float]:
    """Return deterministic pseudo-signal values without randomness."""

    values: list[float] = []
    for index in range(count):
        raw = ((seed + 101) * (index + 1) * (index + 7) + 37) % 100000
        values.append(round(offset + (raw / 100000.0), 6))
    return values


def run_id_for_payload(*, seed: int, count: int, offset: float) -> str:
    payload = {"seed": seed, "count": count, "offset": offset}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"stub_{digest}"


def series_checksum(values: list[float]) -> str:
    canonical = json.dumps(values, sort_keys=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
