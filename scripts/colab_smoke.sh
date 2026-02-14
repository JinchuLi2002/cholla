#!/usr/bin/env bash
set -euo pipefail
set -E

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

mkdir -p artifacts

SCRIPT_STATUS="failed"
EXIT_CODE=1
ERROR_MESSAGE=""
VALIDATOR_PASSED="false"
VALIDATOR_DETAILS="not run"
VALIDATOR_CMD_USED=""
BUILD_CMD_USED='CHOLLA_MACHINE=github make TYPE=cosmology -j2'
RUN_CMD_USED=""
BINARY_PATH=""
RUN_ID=""
RUN_DIR=""
RUN_OUTDIR=""
RUN_LOG=""
RUN_PARAMS=""
SCHEDULE_PATH=""
GIT_SHA_SHORT="unknown"
INPUT_HASH="unknown"

on_err() {
  local code=$?
  ERROR_MESSAGE="Command failed (exit ${code}): ${BASH_COMMAND}"
}
trap on_err ERR

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

sha256_stream() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    echo "no_sha256_tool"
    return 1
  fi
}


            write_env_colab() {
              local git_sha_full="unknown"
              if git rev-parse HEAD >/dev/null 2>&1; then
                git_sha_full="$(git rev-parse HEAD)"
              fi

              local nvidia_name="unknown"
              local nvidia_driver="unknown"
              local nvidia_cuda="unknown"
              local nvidia_summary="nvidia-smi unavailable"

              if command -v nvidia-smi >/dev/null 2>&1; then
                local query
                query="$(nvidia-smi --query-gpu=name,driver_version,cuda_version --format=csv,noheader 2>/dev/null | head -n 1 || true)"
                if [[ -n "${query}" ]]; then
                  IFS=',' read -r nvidia_name nvidia_driver nvidia_cuda <<<"${query}"
                  nvidia_name="$(echo "${nvidia_name}" | xargs)"
                  nvidia_driver="$(echo "${nvidia_driver}" | xargs)"
                  nvidia_cuda="$(echo "${nvidia_cuda}" | xargs)"
                  nvidia_summary="name=${nvidia_name};driver=${nvidia_driver};cuda=${nvidia_cuda}"
                else
                  nvidia_summary="$(nvidia-smi 2>/dev/null | sed -n '1,20p' | tr '
' '; ' || true)"
                fi
              fi

              local gcc_version
              gcc_version="$(gcc --version 2>/dev/null | head -n 1 || echo "gcc unavailable")"

              local os_release
              if [[ -f /etc/os-release ]]; then
                os_release="$(grep -E '^(PRETTY_NAME|NAME|VERSION)=' /etc/os-release | tr '
' '; ' | sed 's/; $//')"
              else
                os_release="$(uname -a)"
              fi

              local cholla_machine_val="${CHOLLA_MACHINE:-github}"

              GIT_SHA_FULL="${git_sha_full}"               NVIDIA_SUMMARY="${nvidia_summary}"               NVIDIA_NAME="${nvidia_name}"               NVIDIA_DRIVER="${nvidia_driver}"               NVIDIA_CUDA="${nvidia_cuda}"               GCC_VERSION_STR="${gcc_version}"               OS_RELEASE_STR="${os_release}"               CHOLLA_MACHINE_VAL="${cholla_machine_val}"               python3 - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "git_sha": os.environ.get("GIT_SHA_FULL", "unknown"),
    "nvidia_smi_summary": os.environ.get("NVIDIA_SUMMARY", ""),
    "gpu_name": os.environ.get("NVIDIA_NAME", "unknown"),
    "driver_version": os.environ.get("NVIDIA_DRIVER", "unknown"),
    "cuda_version": os.environ.get("NVIDIA_CUDA", "unknown"),
    "gcc_version": os.environ.get("GCC_VERSION_STR", ""),
    "os_release": os.environ.get("OS_RELEASE_STR", ""),
    "cholla_machine": os.environ.get("CHOLLA_MACHINE_VAL", ""),
}

Path("artifacts").mkdir(parents=True, exist_ok=True)
Path("artifacts/env_colab.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
            }

write_manifest() {
  local code=$?
  EXIT_CODE="${code}"

  if [[ "${code}" -eq 0 ]]; then
    SCRIPT_STATUS="success"
    if [[ -z "${ERROR_MESSAGE}" ]]; then
      ERROR_MESSAGE=""
    fi
  else
    SCRIPT_STATUS="failed"
    if [[ -z "${ERROR_MESSAGE}" ]]; then
      ERROR_MESSAGE="Script exited with code ${code}"
    fi
  fi

  local produced_files_file
  produced_files_file="$(mktemp)"
  if [[ -n "${RUN_DIR}" && -d "${RUN_DIR}" ]]; then
    find "${RUN_DIR}" -type f | sort >"${produced_files_file}" || true
  else
    : >"${produced_files_file}"
  fi

  local ts_utc
  ts_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  if [[ -n "${RUN_DIR}" ]]; then
    printf "%s\n" "${RUN_DIR}" > artifacts/last_run_dir.txt
  else
    : > artifacts/last_run_dir.txt
  fi

  TS_UTC="${ts_utc}" \
  SCRIPT_STATUS="${SCRIPT_STATUS}" \
  EXIT_CODE="${EXIT_CODE}" \
  ERROR_MESSAGE="${ERROR_MESSAGE}" \
  GIT_SHA_SHORT="${GIT_SHA_SHORT}" \
  INPUT_HASH="${INPUT_HASH}" \
  RUN_ID="${RUN_ID}" \
  RUN_DIR="${RUN_DIR}" \
  RUN_OUTDIR="${RUN_OUTDIR}" \
  RUN_LOG="${RUN_LOG}" \
  RUN_PARAMS="${RUN_PARAMS}" \
  SCHEDULE_PATH="${SCHEDULE_PATH}" \
  BINARY_PATH="${BINARY_PATH}" \
  BUILD_CMD_USED="${BUILD_CMD_USED}" \
  RUN_CMD_USED="${RUN_CMD_USED}" \
  VALIDATOR_PASSED="${VALIDATOR_PASSED}" \
  VALIDATOR_DETAILS="${VALIDATOR_DETAILS}" \
  VALIDATOR_CMD_USED="${VALIDATOR_CMD_USED}" \
  PRODUCED_FILES_FILE="${produced_files_file}" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

def read_lines(path: str):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(line)
    return out

manifest = {
    "git_sha": os.environ["GIT_SHA_SHORT"],
    "build_command": os.environ["BUILD_CMD_USED"],
    "binary_path": os.environ["BINARY_PATH"],
    "params_path": os.environ["RUN_PARAMS"],
    "schedule_path": os.environ["SCHEDULE_PATH"],
    "run_dir": os.environ["RUN_DIR"],
    "produced_files": read_lines(os.environ["PRODUCED_FILES_FILE"]),
    "validation": {
        "status": "pass" if os.environ["VALIDATOR_PASSED"].lower() == "true" else "fail",
        "details": os.environ["VALIDATOR_DETAILS"],
    },
    "error": (None if os.environ["ERROR_MESSAGE"] == "" else os.environ["ERROR_MESSAGE"]),
    "timestamp_utc": os.environ["TS_UTC"],
    "status": os.environ["SCRIPT_STATUS"],
    "exit_code": int(os.environ["EXIT_CODE"]),
    "error_legacy": os.environ["ERROR_MESSAGE"],
    "git_sha_short": os.environ["GIT_SHA_SHORT"],
    "input_hash_sha256": os.environ["INPUT_HASH"],
    "run_id": os.environ["RUN_ID"],
    "run_outdir": os.environ["RUN_OUTDIR"],
    "run_log": os.environ["RUN_LOG"],
    "run_params": os.environ["RUN_PARAMS"],
    "build_cmd": os.environ["BUILD_CMD_USED"],
    "run_cmd": os.environ["RUN_CMD_USED"],
    "validator_passed": os.environ["VALIDATOR_PASSED"].lower() == "true",
    "validator_details": os.environ["VALIDATOR_DETAILS"],
    "validator_cmd": os.environ["VALIDATOR_CMD_USED"],
}

Path("artifacts").mkdir(parents=True, exist_ok=True)
Path("artifacts/run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY

  rm -f "${produced_files_file}"
}
trap write_manifest EXIT

run_validator() {
  local outdir="$1"
  local log_path="$2"

  if [[ -f scripts/validate_smoke.py ]]; then
    VALIDATOR_CMD_USED="python3 scripts/validate_smoke.py --run_dir ${outdir}"
    if python3 scripts/validate_smoke.py --run_dir "${outdir}" >"${RUN_DIR}/validator.log" 2>&1; then
      VALIDATOR_PASSED="true"
      VALIDATOR_DETAILS="python validator passed"
      return 0
    else
      VALIDATOR_PASSED="false"
      VALIDATOR_DETAILS="python validator failed; see ${RUN_DIR}/validator.log and artifacts/validation.json"
      return 1
    fi
  fi

  if [[ -x tests/smoke_cosmo/validate_smoke.sh ]]; then
    VALIDATOR_CMD_USED="tests/smoke_cosmo/validate_smoke.sh ${outdir}"
    if tests/smoke_cosmo/validate_smoke.sh "${outdir}" >"${RUN_DIR}/validator.log" 2>&1; then
      VALIDATOR_PASSED="true"
      VALIDATOR_DETAILS="external shell validator passed"
      return 0
    else
      VALIDATOR_PASSED="false"
      VALIDATOR_DETAILS="external shell validator failed; see ${RUN_DIR}/validator.log"
      return 1
    fi
  fi

  if [[ -f tests/smoke_cosmo/validate_smoke.py ]]; then
    VALIDATOR_CMD_USED="python3 tests/smoke_cosmo/validate_smoke.py ${outdir}"
    if python3 tests/smoke_cosmo/validate_smoke.py "${outdir}" >"${RUN_DIR}/validator.log" 2>&1; then
      VALIDATOR_PASSED="true"
      VALIDATOR_DETAILS="external python validator passed"
      return 0
    else
      VALIDATOR_PASSED="false"
      VALIDATOR_DETAILS="external python validator failed; see ${RUN_DIR}/validator.log"
      return 1
    fi
  fi

  VALIDATOR_CMD_USED="builtin_validator"
  local snapshot_count
  snapshot_count="$(find "${outdir}" -type f \( -name "*.h5*" -o -name "*.bin*" -o -name "*.txt*" \) 2>/dev/null | wc -l | tr -d ' ')"

  if [[ "${snapshot_count}" -ge 1 ]] && grep -q "Saving Snapshot" "${log_path}" 2>/dev/null; then
    VALIDATOR_PASSED="true"
    VALIDATOR_DETAILS="builtin validator passed (found ${snapshot_count} output files and snapshot log markers)"
    return 0
  fi

  VALIDATOR_PASSED="false"
  VALIDATOR_DETAILS="builtin validator failed (found ${snapshot_count} output files)"
  return 1
}

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT_SHA_SHORT="$(git rev-parse --short=12 HEAD)"
fi

write_env_colab

PARAMS_SRC="tests/smoke_cosmo/params.txt"
SCALE_SRC="tests/smoke_cosmo/scale_outputs.txt"
SCHEDULE_PATH="${SCALE_SRC}"

if [[ ! -f "${PARAMS_SRC}" ]]; then
  echo "Missing ${PARAMS_SRC}" >&2
  exit 2
fi
if [[ ! -f "${SCALE_SRC}" ]]; then
  echo "Missing ${SCALE_SRC}" >&2
  exit 2
fi

INPUT_HASH="$(cat "${PARAMS_SRC}" "${SCALE_SRC}" | sha256_stream)"
RUN_ID="${GIT_SHA_SHORT}_${INPUT_HASH:0:16}"
RUN_DIR="runs/smoke_cosmo/${RUN_ID}"
RUN_OUTDIR="${RUN_DIR}"
RUN_LOG="${RUN_DIR}/run.log"
RUN_PARAMS="${RUN_DIR}/params.run.txt"

mkdir -p "${RUN_DIR}"
cp "${PARAMS_SRC}" "${RUN_DIR}/params.txt"
cp "${SCALE_SRC}" "${RUN_DIR}/scale_outputs.txt"
cp tests/smoke_cosmo/README.md "${RUN_DIR}/README.snapshot.txt"

cp "${PARAMS_SRC}" "${RUN_PARAMS}"
sed -i.bak "s|RUN_OUTDIR|./${RUN_OUTDIR}/|g" "${RUN_PARAMS}"
sed -i.bak "s|__RUN_ID__|${RUN_ID}|g" "${RUN_PARAMS}"
rm -f "${RUN_PARAMS}.bak"

EXPECTED_BIN="bin/cholla.cosmology.github"
if [[ -x "${EXPECTED_BIN}" ]]; then
  BINARY_PATH="${EXPECTED_BIN}"
else
  env CHOLLA_MACHINE=github make TYPE=cosmology -j2

  if [[ -x "${EXPECTED_BIN}" ]]; then
    BINARY_PATH="${EXPECTED_BIN}"
  else
    BINARY_PATH="$(find bin -maxdepth 1 -type f -perm -u+x -name 'cholla.cosmology.*' | sort | head -n 1 || true)"
    if [[ -z "${BINARY_PATH}" ]]; then
      echo "Could not locate cosmology binary after build." >&2
      exit 3
    fi
  fi
fi

RUN_CMD_USED="${BINARY_PATH} ${RUN_PARAMS}"
"${BINARY_PATH}" "${RUN_PARAMS}" >"${RUN_LOG}" 2>&1

run_validator "${RUN_OUTDIR}" "${RUN_LOG}" || {
  ERROR_MESSAGE="Validation failed: ${VALIDATOR_DETAILS}"
  exit 4
}

exit 0
