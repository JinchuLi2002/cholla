#!/usr/bin/env python3
"""Deterministic Tier 4 proposer for smoke-parameter sweeps.

Given (seed, iteration), produce a parameter dictionary that:
- uses only sweepable params from agent/spec/param_space_v0.yaml
- respects the safety constraints used by the smoke agent loop
- is deterministic across runs (no hidden randomness)
"""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any


SWEEP_KEYS = (
    "nx",
    "ny",
    "nz",
    "gamma",
    "Init_redshift",
    "H0",
    "Omega_M",
    "Omega_L",
)

GRID_VALUES = (8, 10, 12, 14, 16)

GAMMA_BOUNDS = (1.05, 1.8)
INIT_REDSHIFT_BOUNDS = (0.0, 0.5)
H0_BOUNDS = (60.0, 75.0)
OMEGA_M_BOUNDS = (0.2, 0.4)
OMEGA_L_BOUNDS = (0.6, 0.8)
OMEGA_SUM_BOUNDS = (0.95, 1.05)


def _u01(seed: int, iteration: int, name: str) -> float:
    """Stable U[0,1) variate from SHA-256(seed, iteration, name)."""
    payload = f"{seed}:{iteration}:{name}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    n = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return n / float(2**64)


def _sample_float(lo: float, hi: float, u: float) -> float:
    return lo + (hi - lo) * u


def _sample_discrete(values: tuple[int, ...], u: float) -> int:
    idx = min(int(u * len(values)), len(values) - 1)
    return values[idx]


def _validate(proposal: dict[str, Any]) -> None:
    if tuple(proposal.keys()) != SWEEP_KEYS:
        raise ValueError("proposal keys must match SWEEP_KEYS in order")

    nx = int(proposal["nx"])
    ny = int(proposal["ny"])
    nz = int(proposal["nz"])
    gamma = float(proposal["gamma"])
    init_redshift = float(proposal["Init_redshift"])
    h0 = float(proposal["H0"])
    omega_m = float(proposal["Omega_M"])
    omega_l = float(proposal["Omega_L"])

    if not (nx == ny == nz):
        raise ValueError("grid must be cubic (nx == ny == nz)")
    if nx * ny * nz > 4096:
        raise ValueError("max_total_cells constraint violated")
    if gamma <= 1.0:
        raise ValueError("gamma must be > 1.0")
    if not (INIT_REDSHIFT_BOUNDS[0] <= init_redshift <= INIT_REDSHIFT_BOUNDS[1]):
        raise ValueError("Init_redshift out of bounds")
    if not (H0_BOUNDS[0] <= h0 <= H0_BOUNDS[1]):
        raise ValueError("H0 out of bounds")
    if not (OMEGA_M_BOUNDS[0] <= omega_m <= OMEGA_M_BOUNDS[1]):
        raise ValueError("Omega_M out of bounds")
    if not (OMEGA_L_BOUNDS[0] <= omega_l <= OMEGA_L_BOUNDS[1]):
        raise ValueError("Omega_L out of bounds")
    if abs(omega_m + omega_l - 1.0) > 0.05:
        raise ValueError("omega_sum_near_unity constraint violated")


def propose(seed: int, iteration: int) -> dict[str, Any]:
    """Deterministically propose sweepable smoke params for (seed, iteration)."""
    if iteration < 0:
        raise ValueError("iteration must be >= 0")

    side = _sample_discrete(GRID_VALUES, _u01(seed, iteration, "grid_side"))

    gamma = round(
        _sample_float(*GAMMA_BOUNDS, _u01(seed, iteration, "gamma")),
        8,
    )
    init_redshift = round(
        _sample_float(*INIT_REDSHIFT_BOUNDS, _u01(seed, iteration, "Init_redshift")),
        8,
    )
    h0 = round(
        _sample_float(*H0_BOUNDS, _u01(seed, iteration, "H0")),
        8,
    )

    omega_m = round(
        _sample_float(*OMEGA_M_BOUNDS, _u01(seed, iteration, "Omega_M")),
        8,
    )

    # Enforce abs(Omega_M + Omega_L - 1.0) <= 0.05 and Omega_L bounds exactly.
    sum_min = max(OMEGA_SUM_BOUNDS[0], omega_m + OMEGA_L_BOUNDS[0])
    sum_max = min(OMEGA_SUM_BOUNDS[1], omega_m + OMEGA_L_BOUNDS[1])
    omega_sum = _sample_float(sum_min, sum_max, _u01(seed, iteration, "Omega_sum"))
    omega_l = round(omega_sum - omega_m, 8)

    proposal = {
        "nx": side,
        "ny": side,
        "nz": side,
        "gamma": gamma,
        "Init_redshift": init_redshift,
        "H0": h0,
        "Omega_M": omega_m,
        "Omega_L": omega_l,
    }

    _validate(proposal)
    return proposal


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic smoke parameter proposer.")
    parser.add_argument("--seed", type=int, required=True, help="Deterministic seed.")
    parser.add_argument("--iter", type=int, required=True, help="Iteration index (>=0).")
    args = parser.parse_args()

    payload = propose(seed=args.seed, iteration=args.iter)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
