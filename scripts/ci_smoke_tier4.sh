#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

HISTORY_PATH="agent/history/history.jsonl"
AGENT_RUN_ID="ci_smoke_seed123"

history_line_count() {
  local path="$1"
  python3 - "$path" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if not path.exists():
    print(0)
    raise SystemExit(0)

count = 0
for line in path.read_text(encoding="utf-8").splitlines():
    if line.strip():
        count += 1
print(count)
PY
}

echo "[ci-smoke] step 1/3: override backend smoke test"
bash scripts/test_smoke_overrides.sh
echo "[ci-smoke] PASS: scripts/test_smoke_overrides.sh"

before_count="$(history_line_count "${HISTORY_PATH}")"
echo "[ci-smoke] history lines before agent run: ${before_count}"

echo "[ci-smoke] step 2/3: tier4 agent 3-iteration loop"
python3 agent/run_agent.py --seed 123 --iters 3 --agent-run-id "${AGENT_RUN_ID}"
echo "[ci-smoke] PASS: agent/run_agent.py --seed 123 --iters 3"

after_count="$(history_line_count "${HISTORY_PATH}")"
echo "[ci-smoke] history lines after agent run: ${after_count}"

new_entries=0
if [[ "${after_count}" -ge "${before_count}" ]]; then
  new_entries="$((after_count - before_count))"
else
  # History file was reset/truncated during run; treat current length as new entries.
  new_entries="${after_count}"
fi

if [[ "${new_entries}" -lt 3 ]]; then
  echo "[ci-smoke] ERROR: expected >=3 new history entries, got ${new_entries} (before=${before_count}, after=${after_count})" >&2
  exit 1
fi

echo "[ci-smoke] step 3/3: history assertion"
echo "[ci-smoke] PASS: history entries added >= 3 (new=${new_entries}, before=${before_count}, after=${after_count})"
echo "[ci-smoke] PASS: Tier4 smoke CI entrypoint completed"
