#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/m1_verify.sh --baseline-sha <git_sha_or_ref> --m1-sha <git_sha_or_ref> [options]

Options:
  --config <path>         Config path relative to repo root (default: configs/phase3c_ci_smoke.yaml)
  --max-diffs <n>         Max diff lines from diff_bundle.py (default: 20)
  --ignore <rule>         Extra diff_bundle ignore rule (repeatable)
  --induce-mismatch       After PASS, mutate a copied bundle and verify FAIL path with reasons
  --keep-tmp              Keep temporary worktree directory for inspection
  -h, --help              Show this help

Notes:
  - This script does not run `git pull` or `git push`.
  - It uses detached git worktrees to avoid touching your current checkout state.
EOF
}

BASELINE_SHA=""
M1_SHA=""
CONFIG_REL="configs/phase3c_ci_smoke.yaml"
MAX_DIFFS="20"
INDUCE_MISMATCH="0"
KEEP_TMP="0"
DIFF_IGNORES=("file:config_effective.yaml" "file:controller/config_effective.yaml")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --baseline-sha)
      BASELINE_SHA="${2:-}"
      shift 2
      ;;
    --m1-sha)
      M1_SHA="${2:-}"
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

if [[ -z "$BASELINE_SHA" || -z "$M1_SHA" ]]; then
  echo "ERROR: --baseline-sha and --m1-sha are required" >&2
  usage
  exit 2
fi

if ! [[ "$MAX_DIFFS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --max-diffs must be a non-negative integer" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/cholla_m1_verify.XXXXXX")"
BASELINE_WT="$TMP_ROOT/baseline_wt"
M1_WT="$TMP_ROOT/m1_wt"

cleanup() {
  set +e
  if git -C "$REPO_ROOT" worktree list --porcelain | grep -Fq "worktree $BASELINE_WT"; then
    git -C "$REPO_ROOT" worktree remove --force "$BASELINE_WT" >/dev/null 2>&1
  fi
  if git -C "$REPO_ROOT" worktree list --porcelain | grep -Fq "worktree $M1_WT"; then
    git -C "$REPO_ROOT" worktree remove --force "$M1_WT" >/dev/null 2>&1
  fi
  if [[ "$KEEP_TMP" == "0" ]]; then
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT

acceptance_log_baseline="$TMP_ROOT/baseline_acceptance.log"
acceptance_log_m1="$TMP_ROOT/m1_acceptance.log"
diff_log="$TMP_ROOT/diff.log"

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

echo "[M1-VERIFY] baseline ref: $BASELINE_SHA"
echo "[M1-VERIFY] m1 ref:       $M1_SHA"
echo "[M1-VERIFY] config:       $CONFIG_REL"
echo "[M1-VERIFY] ignores:      ${DIFF_IGNORES[*]}"
echo "[M1-VERIFY] temp dir:     $TMP_ROOT"

echo "[M1-VERIFY] creating baseline worktree"
git -C "$REPO_ROOT" worktree add --detach "$BASELINE_WT" "$BASELINE_SHA" >/dev/null

echo "[M1-VERIFY] creating m1 worktree"
git -C "$REPO_ROOT" worktree add --detach "$M1_WT" "$M1_SHA" >/dev/null

echo "[M1-VERIFY] running baseline acceptance"
run_acceptance "$BASELINE_WT" "$acceptance_log_baseline"

echo "[M1-VERIFY] running m1 acceptance"
run_acceptance "$M1_WT" "$acceptance_log_m1"

BUNDLE_A="$(extract_bundle_path "$acceptance_log_baseline")"
BUNDLE_B="$(extract_bundle_path "$acceptance_log_m1")"

if [[ ! -d "$BUNDLE_A" ]]; then
  echo "FAIL: baseline bundle directory not found: $BUNDLE_A"
  exit 1
fi
if [[ ! -d "$BUNDLE_B" ]]; then
  echo "FAIL: m1 bundle directory not found: $BUNDLE_B"
  exit 1
fi

echo "[M1-VERIFY] baseline bundle: $BUNDLE_A"
echo "[M1-VERIFY] m1 bundle:       $BUNDLE_B"
echo "[M1-VERIFY] running diff_bundle"

diff_cmd=(python "$REPO_ROOT/scripts/diff_bundle.py" --a "$BUNDLE_A" --b "$BUNDLE_B" --max-diffs "$MAX_DIFFS")
for rule in "${DIFF_IGNORES[@]}"; do
  diff_cmd+=(--ignore "$rule")
done

if "${diff_cmd[@]}" >"$diff_log" 2>&1; then
  echo "PASS: bundle parity verified"
else
  echo "FAIL: bundle parity mismatch"
  cat "$diff_log"
  exit 1
fi

if [[ "$INDUCE_MISMATCH" == "1" ]]; then
  echo "[M1-VERIFY] inducing mismatch to verify FAIL reporting"
  MISMATCH_BUNDLE="$TMP_ROOT/mismatch_bundle"
  cp -R "$BUNDLE_B" "$MISMATCH_BUNDLE"

  python - "$MISMATCH_BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
summary_path = bundle / "summary.json"
if not summary_path.exists():
    raise SystemExit(f"missing summary.json at {summary_path}")
payload = json.loads(summary_path.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("summary.json payload must be an object")
payload["m1_verify_induced_mismatch"] = True
summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

  mismatch_diff_log="$TMP_ROOT/diff_induced.log"
  mismatch_diff_cmd=(python "$REPO_ROOT/scripts/diff_bundle.py" --a "$BUNDLE_A" --b "$MISMATCH_BUNDLE" --max-diffs "$MAX_DIFFS")
  for rule in "${DIFF_IGNORES[@]}"; do
    mismatch_diff_cmd+=(--ignore "$rule")
  done
  if "${mismatch_diff_cmd[@]}" >"$mismatch_diff_log" 2>&1; then
    echo "FAIL: induced mismatch did not trigger diff failure"
    cat "$mismatch_diff_log"
    exit 1
  fi
  echo "PASS: induced mismatch produced FAIL as expected"
  cat "$mismatch_diff_log"
fi

echo "[M1-VERIFY] baseline acceptance tail (last 30 lines)"
tail -n 30 "$acceptance_log_baseline"
echo "[M1-VERIFY] m1 acceptance tail (last 30 lines)"
tail -n 30 "$acceptance_log_m1"
