#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/m3_stub_verify.sh [options]

Options:
  --mode <same_sha>      Verification mode (default: same_sha)
  --config <path>        Stub config path (default: configs/stub.yaml)
  --plan <path>          Stub PlanSpec path (default: plans/stub_plan.json)
  --max-diffs <n>        Max diff lines (default: 20)
  --keep-tmp             Keep temp directory
  -h, --help             Show help

Behavior:
  - Runs stub domain twice on the same SHA.
  - Captures first and second controller bundles.
  - Diffs bundles with NO ignores via scripts/diff_bundle.py.
EOF
}

MODE="same_sha"
CONFIG_REL="configs/stub.yaml"
PLAN_REL="plans/stub_plan.json"
MAX_DIFFS="20"
KEEP_TMP="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --config)
      CONFIG_REL="${2:-}"
      shift 2
      ;;
    --plan)
      PLAN_REL="${2:-}"
      shift 2
      ;;
    --max-diffs)
      MAX_DIFFS="${2:-}"
      shift 2
      ;;
    --keep-tmp)
      KEEP_TMP="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "$MODE" != "same_sha" ]]; then
  echo "ERROR: unsupported mode: $MODE (expected same_sha)" >&2
  exit 2
fi

if ! [[ "$MAX_DIFFS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --max-diffs must be a non-negative integer" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [[ ! -f "$CONFIG_REL" ]]; then
  echo "ERROR: missing config: $CONFIG_REL" >&2
  exit 2
fi
if [[ ! -f "$PLAN_REL" ]]; then
  echo "ERROR: missing plan: $PLAN_REL" >&2
  exit 2
fi

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/cholla_m3_stub_verify.XXXXXX")"
BUNDLE_ROOT="$TMP_ROOT/bundle_root"
RUN1_LOG="$TMP_ROOT/run1.log"
RUN2_LOG="$TMP_ROOT/run2.log"
DIFF_LOG="$TMP_ROOT/diff.log"
BUNDLE_A="$TMP_ROOT/bundle_a"
BUNDLE_B="$TMP_ROOT/bundle_b"

cleanup() {
  if [[ "$KEEP_TMP" == "0" ]]; then
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT

extract_controller_bundle_path() {
  local log_path="$1"
  python - "$log_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
bundle = ""
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line.startswith("{"):
        continue
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        continue
    candidate = payload.get("controller_bundle_path")
    if isinstance(candidate, str) and candidate:
        bundle = candidate
if not bundle:
    raise SystemExit(1)
print(bundle)
PY
}

extract_metric_scalar() {
  local bundle_dir="$1"
  python - "$bundle_dir" <<'PY'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
iter0 = bundle / "iterations" / "iter_0.json"
payload = json.loads(iter0.read_text(encoding="utf-8"))
tool_results = payload.get("tool_results", [])
for call in tool_results:
    if not isinstance(call, dict):
        continue
    result = call.get("result")
    if not isinstance(result, dict):
        continue
    scalar = result.get("scalar")
    if isinstance(scalar, (int, float)) and not isinstance(scalar, bool):
        print(float(scalar))
        raise SystemExit(0)
print("nan")
PY
}

extract_replay_status() {
  local bundle_dir="$1"
  python - "$bundle_dir" <<'PY'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
report = bundle / "replay" / "strict" / "report.json"
payload = json.loads(report.read_text(encoding="utf-8"))
status = payload.get("status")
if not isinstance(status, str) or not status:
    raise SystemExit(1)
print(status)
PY
}

run_once() {
  local log_path="$1"
  python -m kernel.runner \
    --domain stub \
    --config "$CONFIG_REL" \
    --plan "$PLAN_REL" \
    --bundle-root "$BUNDLE_ROOT" \
    --acceptance_replay_strict >"$log_path" 2>&1
}

echo "[M3-STUB-VERIFY] mode: $MODE"
echo "[M3-STUB-VERIFY] config: $CONFIG_REL"
echo "[M3-STUB-VERIFY] plan: $PLAN_REL"
echo "[M3-STUB-VERIFY] tmp: $TMP_ROOT"

run_once "$RUN1_LOG"
CTRL_BUNDLE_1="$(extract_controller_bundle_path "$RUN1_LOG")"
cp -R "$CTRL_BUNDLE_1" "$BUNDLE_A"

run_once "$RUN2_LOG"
CTRL_BUNDLE_2="$(extract_controller_bundle_path "$RUN2_LOG")"
cp -R "$CTRL_BUNDLE_2" "$BUNDLE_B"

METRIC_A="$(extract_metric_scalar "$BUNDLE_A")"
METRIC_B="$(extract_metric_scalar "$BUNDLE_B")"
REPLAY_A="$(extract_replay_status "$BUNDLE_A")"
REPLAY_B="$(extract_replay_status "$BUNDLE_B")"

echo "[M3-STUB-VERIFY] metric run1: $METRIC_A"
echo "[M3-STUB-VERIFY] metric run2: $METRIC_B"
echo "[M3-STUB-VERIFY] replay run1: $REPLAY_A"
echo "[M3-STUB-VERIFY] replay run2: $REPLAY_B"

if [[ "$METRIC_A" != "$METRIC_B" ]]; then
  echo "FAIL: metric scalar mismatch across reruns" >&2
  exit 1
fi
if [[ "$REPLAY_A" != "ok" || "$REPLAY_B" != "ok" ]]; then
  echo "FAIL: strict replay status not ok across reruns" >&2
  exit 1
fi

if python scripts/diff_bundle.py --a "$BUNDLE_A" --b "$BUNDLE_B" --max-diffs "$MAX_DIFFS" >"$DIFF_LOG" 2>&1; then
  echo "PASS: same_sha rerun parity verified (no ignores)"
else
  echo "FAIL: same_sha rerun parity mismatch"
  cat "$DIFF_LOG"
  exit 1
fi

echo "[M3-STUB-VERIFY] diff_bundle output:"
cat "$DIFF_LOG"
