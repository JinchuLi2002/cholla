#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/m2_verify.sh --baseline <git_sha_or_ref> --candidate <git_sha_or_ref> [options]

Options:
  --config <path>         Config path relative to repo root (default: configs/phase3c_ci_smoke.yaml)
  --max-diffs <n>         Max diff lines from diff_bundle.py (default: 20)
  --ignore <rule>         Extra diff_bundle ignore rule (repeatable)
  --induce-mismatch       After parity PASS, mutate candidate bundle copy and verify FAIL path
  --keep-tmp              Keep temporary worktree directory for inspection
  -h, --help              Show this help

Notes:
  - This script does not run `git pull` or `git push`.
  - It uses detached git worktrees to avoid touching your current checkout state.
  - Acceptance command in both worktrees:
      python -m agent.controller.run_phase3c --config <...> --acceptance_replay_strict
EOF
}

BASELINE_SHA=""
CANDIDATE_SHA=""
CONFIG_REL="configs/phase3c_ci_smoke.yaml"
MAX_DIFFS="20"
INDUCE_MISMATCH="0"
KEEP_TMP="0"
DIFF_IGNORES=(
  "file:config_effective.yaml"
  "file:controller/config_effective.yaml"
  "/HEAD_SHA"
  "/head_sha"
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --baseline)
      BASELINE_SHA="${2:-}"
      shift 2
      ;;
    --candidate)
      CANDIDATE_SHA="${2:-}"
      shift 2
      ;;
    --config)
      CONFIG_REL="${2:-}"
      shift 2
      ;;
    --max-diffs)
      MAX_DIFFS="${2:-}"
      shift 2
      ;;
    --ignore)
      DIFF_IGNORES+=("${2:-}")
      shift 2
      ;;
    --induce-mismatch)
      INDUCE_MISMATCH="1"
      shift
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

if [[ -z "$BASELINE_SHA" || -z "$CANDIDATE_SHA" ]]; then
  echo "ERROR: --baseline and --candidate are required" >&2
  usage
  exit 2
fi

if ! [[ "$MAX_DIFFS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --max-diffs must be a non-negative integer" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/cholla_m2_verify.XXXXXX")"
BASELINE_WT="$TMP_ROOT/baseline_wt"
CANDIDATE_WT="$TMP_ROOT/candidate_wt"

cleanup() {
  set +e
  if git -C "$REPO_ROOT" worktree list --porcelain | grep -Fq "worktree $BASELINE_WT"; then
    git -C "$REPO_ROOT" worktree remove --force "$BASELINE_WT" >/dev/null 2>&1
  fi
  if git -C "$REPO_ROOT" worktree list --porcelain | grep -Fq "worktree $CANDIDATE_WT"; then
    git -C "$REPO_ROOT" worktree remove --force "$CANDIDATE_WT" >/dev/null 2>&1
  fi
  if [[ "$KEEP_TMP" == "0" ]]; then
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT

ACCEPTANCE_LOG_BASELINE="$TMP_ROOT/baseline_acceptance.log"
ACCEPTANCE_LOG_CANDIDATE="$TMP_ROOT/candidate_acceptance.log"
DIFF_LOG="$TMP_ROOT/diff.log"

run_acceptance() {
  local wt="$1"
  local log_path="$2"

  if [[ ! -f "$wt/$CONFIG_REL" ]]; then
    echo "ERROR: config not found in worktree: $wt/$CONFIG_REL" >&2
    return 2
  fi

  (
    cd "$wt"
    python -m agent.controller.run_phase3c \
      --config "$CONFIG_REL" \
      --acceptance_replay_strict >"$log_path" 2>&1
  )
}

extract_bundle_path() {
  local log_path="$1"
  python - "$log_path" <<'PY'
import json
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
bundle = ""
for line in log_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(payload, dict) and isinstance(payload.get("experiment_bundle_path"), str):
        bundle = payload["experiment_bundle_path"].strip()
if not bundle:
    raise SystemExit(1)
print(bundle)
PY
}

first_diff_reason() {
  local log_path="$1"
  python - "$log_path" <<'PY'
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
for line in log_path.read_text(encoding="utf-8").splitlines():
    if line.startswith("- "):
        print(line[2:].strip())
        raise SystemExit(0)
print("no diff reason found")
PY
}

echo "[M2-VERIFY] baseline ref:  $BASELINE_SHA"
echo "[M2-VERIFY] candidate ref: $CANDIDATE_SHA"
echo "[M2-VERIFY] config:        $CONFIG_REL"
echo "[M2-VERIFY] ignores:       ${DIFF_IGNORES[*]}"
echo "[M2-VERIFY] temp dir:      $TMP_ROOT"

echo "[M2-VERIFY] creating baseline worktree"
git -C "$REPO_ROOT" worktree add --detach "$BASELINE_WT" "$BASELINE_SHA" >/dev/null

echo "[M2-VERIFY] creating candidate worktree"
git -C "$REPO_ROOT" worktree add --detach "$CANDIDATE_WT" "$CANDIDATE_SHA" >/dev/null

echo "[M2-VERIFY] running baseline acceptance+strict replay"
run_acceptance "$BASELINE_WT" "$ACCEPTANCE_LOG_BASELINE"

echo "[M2-VERIFY] running candidate acceptance+strict replay"
run_acceptance "$CANDIDATE_WT" "$ACCEPTANCE_LOG_CANDIDATE"

BUNDLE_BASELINE="$(extract_bundle_path "$ACCEPTANCE_LOG_BASELINE")"
BUNDLE_CANDIDATE="$(extract_bundle_path "$ACCEPTANCE_LOG_CANDIDATE")"

if [[ ! -d "$BUNDLE_BASELINE" ]]; then
  echo "FAIL: baseline bundle directory not found: $BUNDLE_BASELINE"
  exit 1
fi
if [[ ! -d "$BUNDLE_CANDIDATE" ]]; then
  echo "FAIL: candidate bundle directory not found: $BUNDLE_CANDIDATE"
  exit 1
fi

echo "[M2-VERIFY] baseline bundle:  $BUNDLE_BASELINE"
echo "[M2-VERIFY] candidate bundle: $BUNDLE_CANDIDATE"
echo "[M2-VERIFY] running diff_bundle parity"

diff_cmd=(python "$REPO_ROOT/scripts/diff_bundle.py" --a "$BUNDLE_BASELINE" --b "$BUNDLE_CANDIDATE" --max-diffs "$MAX_DIFFS")
for rule in "${DIFF_IGNORES[@]}"; do
  diff_cmd+=(--ignore "$rule")
done

if "${diff_cmd[@]}" >"$DIFF_LOG" 2>&1; then
  echo "PASS: baseline/candidate bundle parity verified"
else
  echo "FAIL: baseline/candidate bundle parity mismatch"
  first_reason="$(first_diff_reason "$DIFF_LOG")"
  echo "First diff reason: $first_reason"
  cat "$DIFF_LOG"
  exit 1
fi

if [[ "$INDUCE_MISMATCH" == "1" ]]; then
  echo "[M2-VERIFY] inducing mismatch to verify FAIL reporting"
  MISMATCH_BUNDLE="$TMP_ROOT/mismatch_bundle"
  cp -R "$BUNDLE_CANDIDATE" "$MISMATCH_BUNDLE"

  python - "$MISMATCH_BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
summary_path = bundle / "summary.json"
if not summary_path.exists():
    summary_path = bundle / "controller" / "summary.json"
if not summary_path.exists():
    raise SystemExit(f"missing summary.json at {bundle}")

payload = json.loads(summary_path.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("summary.json payload must be an object")
payload["m2_verify_induced_mismatch"] = True
summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

  mismatch_diff_log="$TMP_ROOT/diff_induced.log"
  mismatch_diff_cmd=(python "$REPO_ROOT/scripts/diff_bundle.py" --a "$BUNDLE_BASELINE" --b "$MISMATCH_BUNDLE" --max-diffs "$MAX_DIFFS")
  for rule in "${DIFF_IGNORES[@]}"; do
    mismatch_diff_cmd+=(--ignore "$rule")
  done

  if "${mismatch_diff_cmd[@]}" >"$mismatch_diff_log" 2>&1; then
    echo "FAIL: induced mismatch did not trigger diff failure"
    cat "$mismatch_diff_log"
    exit 1
  fi
  first_reason="$(first_diff_reason "$mismatch_diff_log")"
  echo "FAIL: induced mismatch produced expected parity failure"
  echo "First induced diff reason: $first_reason"
  cat "$mismatch_diff_log"
  exit 1
fi

echo "[M2-VERIFY] baseline acceptance tail (last 30 lines)"
tail -n 30 "$ACCEPTANCE_LOG_BASELINE"
echo "[M2-VERIFY] candidate acceptance tail (last 30 lines)"
tail -n 30 "$ACCEPTANCE_LOG_CANDIDATE"
