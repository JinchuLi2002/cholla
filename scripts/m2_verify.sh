#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/m2_verify.sh --mode <same_sha|cross_sha> --baseline <git_sha_or_ref> --candidate <git_sha_or_ref> [options]

Options:
  --mode <same_sha|cross_sha>
                         Required harness mode:
                           same_sha  => baseline/candidate must resolve to the same commit and ignores must be empty
                           cross_sha => baseline/candidate must resolve to different commits; only commit-id ignores allowed
  --config <path>         Config path relative to repo root (default: configs/phase3c_ci_smoke.yaml)
  --max-diffs <n>         Max diff lines from diff_bundle.py (default: 20)
  --ignore <rule>         Extra JSON pointer ignore rule (repeatable); restricted by --mode policy
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
MODE=""
CONFIG_REL="configs/phase3c_ci_smoke.yaml"
MAX_DIFFS="20"
INDUCE_MISMATCH="0"
KEEP_TMP="0"
COMMIT_ID_JSON_PTRS=(
  "/HEAD_SHA"
  "/head_sha"
)
EXTRA_IGNORES=()
IGNORE_JSON_PTRS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
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
      EXTRA_IGNORES+=("${2:-}")
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

if [[ -z "$MODE" ]]; then
  echo "ERROR: --mode is required (same_sha|cross_sha)" >&2
  usage
  exit 2
fi

if [[ "$MODE" != "same_sha" && "$MODE" != "cross_sha" ]]; then
  echo "ERROR: --mode must be one of: same_sha, cross_sha" >&2
  exit 2
fi

if ! [[ "$MAX_DIFFS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --max-diffs must be a non-negative integer" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

BASELINE_RESOLVED_SHA="$(git -C "$REPO_ROOT" rev-parse "${BASELINE_SHA}^{commit}")"
CANDIDATE_RESOLVED_SHA="$(git -C "$REPO_ROOT" rev-parse "${CANDIDATE_SHA}^{commit}")"

_is_commit_identity_ptr() {
  local ptr="$1"
  case "$ptr" in
    "/HEAD_SHA"|"/head_sha")
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

if [[ "${#EXTRA_IGNORES[@]}" -gt 0 ]]; then
  for rule in "${EXTRA_IGNORES[@]}"; do
    if [[ "$rule" == file:* ]]; then
      echo "ERROR: file-level ignores are not allowed: $rule" >&2
      exit 2
    fi
  done
fi

if [[ "$MODE" == "same_sha" ]]; then
  if [[ "$BASELINE_RESOLVED_SHA" != "$CANDIDATE_RESOLVED_SHA" ]]; then
    echo "ERROR: same_sha mode requires baseline and candidate to resolve to the same commit" >&2
    echo "  baseline=$BASELINE_RESOLVED_SHA" >&2
    echo "  candidate=$CANDIDATE_RESOLVED_SHA" >&2
    exit 2
  fi
  if [[ "${#EXTRA_IGNORES[@]}" -ne 0 ]]; then
    echo "ERROR: same_sha mode forbids --ignore; ignore list must be empty" >&2
    exit 2
  fi
  IGNORE_JSON_PTRS=()
fi

if [[ "$MODE" == "cross_sha" ]]; then
  if [[ "$BASELINE_RESOLVED_SHA" == "$CANDIDATE_RESOLVED_SHA" ]]; then
    echo "ERROR: cross_sha mode requires baseline and candidate to resolve to different commits" >&2
    echo "  baseline=$BASELINE_RESOLVED_SHA" >&2
    echo "  candidate=$CANDIDATE_RESOLVED_SHA" >&2
    exit 2
  fi
  IGNORE_JSON_PTRS=("${COMMIT_ID_JSON_PTRS[@]}")
  if [[ "${#EXTRA_IGNORES[@]}" -gt 0 ]]; then
    for rule in "${EXTRA_IGNORES[@]}"; do
      if ! _is_commit_identity_ptr "$rule"; then
        echo "ERROR: cross_sha mode allows commit-identity ignores only (/HEAD_SHA, /head_sha); got $rule" >&2
        exit 2
      fi
      already_present="0"
      for existing in "${IGNORE_JSON_PTRS[@]}"; do
        if [[ "$existing" == "$rule" ]]; then
          already_present="1"
          break
        fi
      done
      if [[ "$already_present" == "0" ]]; then
        IGNORE_JSON_PTRS+=("$rule")
      fi
    done
  fi
fi

if [[ "$MODE" == "same_sha" && "${#IGNORE_JSON_PTRS[@]}" -ne 0 ]]; then
  echo "ERROR: same_sha mode must run with empty ignore list" >&2
  exit 2
fi

if [[ "$MODE" == "cross_sha" ]]; then
  if [[ "${#IGNORE_JSON_PTRS[@]}" -gt 0 ]]; then
    for rule in "${IGNORE_JSON_PTRS[@]}"; do
      if ! _is_commit_identity_ptr "$rule"; then
        echo "ERROR: cross_sha mode encountered non commit-id ignore rule: $rule" >&2
        exit 2
      fi
    done
  fi
fi

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
echo "[M2-VERIFY] mode:          $MODE"
echo "[M2-VERIFY] baseline sha:  $BASELINE_RESOLVED_SHA"
echo "[M2-VERIFY] candidate sha: $CANDIDATE_RESOLVED_SHA"
echo "[M2-VERIFY] config:        $CONFIG_REL"
echo "[M2-VERIFY] ignores:       ${IGNORE_JSON_PTRS[*]:-<empty>}"
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
if [[ "${#IGNORE_JSON_PTRS[@]}" -gt 0 ]]; then
  for rule in "${IGNORE_JSON_PTRS[@]}"; do
    diff_cmd+=(--ignore "$rule")
  done
fi

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
  if [[ "${#IGNORE_JSON_PTRS[@]}" -gt 0 ]]; then
    for rule in "${IGNORE_JSON_PTRS[@]}"; do
      mismatch_diff_cmd+=(--ignore "$rule")
    done
  fi

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
