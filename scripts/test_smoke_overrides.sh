#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

TMP_PARAMS="${REPO_ROOT}/_tmp_params_variant.txt"
SCHEMA_PATH="${REPO_ROOT}/artifacts_schema/run_manifest_v0.json"

cleanup() {
  rm -f "${TMP_PARAMS}"
}
trap cleanup EXIT

ensure_jsonschema() {
  if python3 - <<'PY' >/dev/null 2>&1
import jsonschema
PY
  then
    return 0
  fi

  python3 -m pip install --user -q jsonschema
  python3 - <<'PY' >/dev/null
import jsonschema
PY
}

validate_manifest_schema() {
  local manifest_path="$1"
  local run_label="$2"
  python3 - "$manifest_path" "$SCHEMA_PATH" "$run_label" <<'PY'
import json
import sys

import jsonschema

manifest_path, schema_path, run_label = sys.argv[1:4]
with open(schema_path, encoding="utf-8") as infile:
    schema = json.load(infile)
with open(manifest_path, encoding="utf-8") as infile:
    manifest = json.load(infile)

jsonschema.validate(instance=manifest, schema=schema)
print(f"{run_label}: manifest schema validation passed")
PY
}

assert_validation_pass() {
  local run_label="$1"
  python3 - "$run_label" <<'PY'
import json
import sys
from pathlib import Path

run_label = sys.argv[1]
path = Path("artifacts/validation.json")
if not path.exists():
    raise SystemExit(f"{run_label}: missing artifacts/validation.json")

payload = json.loads(path.read_text(encoding="utf-8"))
status = payload.get("status")
if status != "pass":
    raise SystemExit(f"{run_label}: validation status is {status!r}, expected 'pass'")

print(f"{run_label}: validation.json status=pass")
PY
}

capture_run_info() {
  local run_dir_var="$1"
  local run_id_var="$2"
  local manifest_copy_var="$3"
  local run_label="$4"

  local run_dir
  run_dir="$(cat artifacts/last_run_dir.txt)"
  if [[ -z "${run_dir}" || ! -d "${run_dir}" ]]; then
    echo "${run_label}: invalid run dir from artifacts/last_run_dir.txt: '${run_dir}'" >&2
    exit 1
  fi

  local run_id
  run_id="$(basename "${run_dir}")"
  if [[ -z "${run_id}" ]]; then
    echo "${run_label}: unable to compute run id from '${run_dir}'" >&2
    exit 1
  fi

  local manifest_copy
  manifest_copy="${run_dir}/run_manifest.test.json"
  cp artifacts/run_manifest.json "${manifest_copy}"

  printf -v "${run_dir_var}" "%s" "${run_dir}"
  printf -v "${run_id_var}" "%s" "${run_id}"
  printf -v "${manifest_copy_var}" "%s" "${manifest_copy}"
}

echo "[default] running scripts/colab_smoke.sh"
bash scripts/colab_smoke.sh

capture_run_info RUN_DIR1 RUN_ID1 MANIFEST1 "default"
echo "[default] RUN_ID=${RUN_ID1}"
echo "[default] RUN_DIR=${RUN_DIR1}"

ensure_jsonschema
validate_manifest_schema "${MANIFEST1}" "default"
assert_validation_pass "default"

cp tests/smoke_cosmo/params.txt "${TMP_PARAMS}"
# Safe hash-changing tweak: append a comment line to variant params.
printf "\n# override-variant marker for test_smoke_overrides\n" >> "${TMP_PARAMS}"

echo "[override] running scripts/colab_smoke.sh --params ${TMP_PARAMS}"
bash scripts/colab_smoke.sh --params "${TMP_PARAMS}"

capture_run_info RUN_DIR2 RUN_ID2 MANIFEST2 "override"
echo "[override] RUN_ID=${RUN_ID2}"
echo "[override] RUN_DIR=${RUN_DIR2}"

validate_manifest_schema "${MANIFEST2}" "override"
assert_validation_pass "override"

if [[ "${RUN_ID1}" == "${RUN_ID2}" ]]; then
  echo "Expected RUN_ID to differ between default and override runs, but both were '${RUN_ID1}'" >&2
  exit 1
fi

echo "PASS: default and override runs succeeded with distinct RUN_IDs"
