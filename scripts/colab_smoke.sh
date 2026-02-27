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
ANALYSIS_SCHEDULE_PATH=""
PARAMS_SOURCE_PATH=""
SCHEDULE_SOURCE_PATH=""
ANALYSIS_SCHEDULE_SOURCE_PATH=""
RUN_INPUT_PARAMS=""
RUN_INPUT_SCHEDULE=""
RUN_INPUT_ANALYSIS_SCHEDULE=""
OUT_ROOT_USED=""
RUN_PRESET="smoke_cosmo"
RUN_PRESET_DIR="tests/smoke_cosmo"
RUN_ID_OVERRIDE=""
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

sed_escape_replacement() {
  printf "%s" "$1" | sed -e 's/[&|]/\\&/g'
}

usage() {
  cat <<'EOF'
Usage: bash scripts/colab_smoke.sh [--preset DIR] [--params PATH] [--schedule PATH] [--analysis-schedule PATH] [--out-root PATH] [--run-id RUN_ID]

Options:
  --preset DIR      Preset directory. Uses DIR/params.txt and DIR/scale_outputs.txt unless overridden (default: tests/smoke_cosmo)
  --params PATH     Parameter template file (default: tests/smoke_cosmo/params.txt)
  --schedule PATH   scale_outputs file (default: tests/smoke_cosmo/scale_outputs.txt)
  --analysis-schedule PATH
                    Optional analysis_scale_outputs file. If provided (or found as DIR/analysis_scale_outputs.txt), it is staged and wired into params.
  --out-root PATH   Root directory for run outputs (default: runs/smoke_cosmo)
  --run-id RUN_ID   Explicit run id. If omitted, run id is computed from effective inputs.
  -h, --help        Show this help text.
EOF
}

parse_args() {
  local params_override=""
  local schedule_override=""
  local analysis_schedule_override=""
  local out_root_override=""

  RUN_PRESET_DIR="tests/smoke_cosmo"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --preset)
        if [[ $# -lt 2 ]]; then
          echo "Missing value for --preset" >&2
          usage >&2
          exit 2
        fi
        RUN_PRESET_DIR="$2"
        shift 2
        ;;
      --params)
        if [[ $# -lt 2 ]]; then
          echo "Missing value for --params" >&2
          usage >&2
          exit 2
        fi
        params_override="$2"
        shift 2
        ;;
      --schedule)
        if [[ $# -lt 2 ]]; then
          echo "Missing value for --schedule" >&2
          usage >&2
          exit 2
        fi
        schedule_override="$2"
        shift 2
        ;;
      --analysis-schedule)
        if [[ $# -lt 2 ]]; then
          echo "Missing value for --analysis-schedule" >&2
          usage >&2
          exit 2
        fi
        analysis_schedule_override="$2"
        shift 2
        ;;
      --out-root)
        if [[ $# -lt 2 ]]; then
          echo "Missing value for --out-root" >&2
          usage >&2
          exit 2
        fi
        out_root_override="$2"
        shift 2
        ;;
      --run-id)
        if [[ $# -lt 2 ]]; then
          echo "Missing value for --run-id" >&2
          usage >&2
          exit 2
        fi
        RUN_ID_OVERRIDE="$2"
        shift 2
        ;;
      -h|--help)
        trap - EXIT
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage >&2
        exit 2
        ;;
    esac
  done

  RUN_PRESET_DIR="${RUN_PRESET_DIR%/}"
  if [[ -z "${RUN_PRESET_DIR}" ]]; then
    echo "Invalid --preset (empty value)" >&2
    exit 2
  fi

  RUN_PRESET="$(basename "${RUN_PRESET_DIR}")"
  if [[ -z "${RUN_PRESET}" || "${RUN_PRESET}" == "." ]]; then
    RUN_PRESET="smoke_cosmo"
  fi

  if [[ -n "${params_override}" ]]; then
    EFFECTIVE_PARAMS="${params_override}"
  else
    EFFECTIVE_PARAMS="${RUN_PRESET_DIR}/params.txt"
  fi

  if [[ -n "${schedule_override}" ]]; then
    EFFECTIVE_SCHEDULE="${schedule_override}"
  else
    EFFECTIVE_SCHEDULE="${RUN_PRESET_DIR}/scale_outputs.txt"
  fi

  if [[ -n "${analysis_schedule_override}" ]]; then
    EFFECTIVE_ANALYSIS_SCHEDULE="${analysis_schedule_override}"
  elif [[ -f "${RUN_PRESET_DIR}/analysis_scale_outputs.txt" ]]; then
    EFFECTIVE_ANALYSIS_SCHEDULE="${RUN_PRESET_DIR}/analysis_scale_outputs.txt"
  else
    EFFECTIVE_ANALYSIS_SCHEDULE=""
  fi

  if [[ -n "${out_root_override}" ]]; then
    OUT_ROOT_USED="${out_root_override}"
  else
    OUT_ROOT_USED="runs/${RUN_PRESET}"
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
    local nvidia_raw
    nvidia_raw="$(nvidia-smi 2>/dev/null || true)"
    if [[ -n "${nvidia_raw}" ]]; then
      nvidia_summary="$(printf "%s" "${nvidia_raw}" | sed -n '1,20p' | tr '\n' '; ' | sed 's/; $//')"
    fi

    # `cuda_version` is not available in all nvidia-smi query versions.
    local query
    query="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -n 1 || true)"
    if [[ -n "${query}" ]]; then
      IFS=',' read -r nvidia_name nvidia_driver <<<"${query}"
      nvidia_name="$(echo "${nvidia_name}" | xargs)"
      nvidia_driver="$(echo "${nvidia_driver}" | xargs)"
    fi

    local cuda_from_header
    cuda_from_header="$(printf "%s\n" "${nvidia_raw}" | sed -n 's/.*CUDA Version: \([^ ]*\).*/\1/p' | head -n 1)"
    if [[ -n "${cuda_from_header}" ]]; then
      nvidia_cuda="${cuda_from_header}"
    fi
  fi

  if [[ "${nvidia_cuda}" == "unknown" ]] && command -v nvcc >/dev/null 2>&1; then
    local nvcc_cuda
    nvcc_cuda="$(nvcc --version 2>/dev/null | sed -n 's/.*release \([0-9.]*\),.*/\1/p' | head -n 1 || true)"
    if [[ -n "${nvcc_cuda}" ]]; then
      nvidia_cuda="${nvcc_cuda}"
    fi
  fi

  local gcc_version
  gcc_version="$(gcc --version 2>/dev/null | head -n 1 || echo "gcc unavailable")"

  local os_release
  if [[ -f /etc/os-release ]]; then
    os_release="$(grep -E '^(PRETTY_NAME|NAME|VERSION)=' /etc/os-release | tr '\n' '; ' | sed 's/; $//')"
  else
    os_release="$(uname -a)"
  fi

  local cholla_machine_val="${CHOLLA_MACHINE:-github}"

  GIT_SHA_FULL="${git_sha_full}" \
  NVIDIA_SUMMARY="${nvidia_summary}" \
  NVIDIA_NAME="${nvidia_name}" \
  NVIDIA_DRIVER="${nvidia_driver}" \
  NVIDIA_CUDA="${nvidia_cuda}" \
  GCC_VERSION_STR="${gcc_version}" \
  OS_RELEASE_STR="${os_release}" \
  CHOLLA_MACHINE_VAL="${cholla_machine_val}" \
  python3 - <<'PY'
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

  local validation_global_path="artifacts/validation.json"
  local validation_run_path=""
  if [[ -n "${RUN_DIR}" ]]; then
    validation_run_path="${RUN_DIR}/validator.json"
  fi

  if [[ ! -f "${validation_global_path}" ]]; then
    local validation_status="fail"
    if [[ "${VALIDATOR_PASSED}" == "true" ]]; then
      validation_status="pass"
    fi

    VALIDATION_STATUS="${validation_status}" \
    VALIDATION_DETAILS="${VALIDATOR_DETAILS}" \
    VALIDATION_CMD="${VALIDATOR_CMD_USED}" \
    VALIDATION_RUN_DIR="${RUN_DIR}" \
    VALIDATION_RUN_LOG="${RUN_LOG}" \
    VALIDATION_TIMESTAMP="${ts_utc}" \
    VALIDATION_GLOBAL_PATH="${validation_global_path}" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "run_dir": os.environ["VALIDATION_RUN_DIR"],
    "run_log": os.environ["VALIDATION_RUN_LOG"],
    "status": os.environ["VALIDATION_STATUS"],
    "details": os.environ["VALIDATION_DETAILS"],
    "validator_cmd": os.environ["VALIDATION_CMD"],
    "timestamp": os.environ["VALIDATION_TIMESTAMP"],
}

path = Path(os.environ["VALIDATION_GLOBAL_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
  fi

  if [[ -n "${validation_run_path}" && -f "${validation_global_path}" ]]; then
    mkdir -p "$(dirname "${validation_run_path}")"
    cp "${validation_global_path}" "${validation_run_path}" || true
  fi

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
  ANALYSIS_SCHEDULE_PATH="${ANALYSIS_SCHEDULE_PATH}" \
  PARAMS_SOURCE_PATH="${PARAMS_SOURCE_PATH}" \
  SCHEDULE_SOURCE_PATH="${SCHEDULE_SOURCE_PATH}" \
  ANALYSIS_SCHEDULE_SOURCE_PATH="${ANALYSIS_SCHEDULE_SOURCE_PATH}" \
  RUN_INPUT_PARAMS="${RUN_INPUT_PARAMS}" \
  RUN_INPUT_SCHEDULE="${RUN_INPUT_SCHEDULE}" \
  RUN_INPUT_ANALYSIS_SCHEDULE="${RUN_INPUT_ANALYSIS_SCHEDULE}" \
  OUT_ROOT_USED="${OUT_ROOT_USED}" \
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
    "analysis_schedule_path": os.environ["ANALYSIS_SCHEDULE_PATH"],
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
    # Canonical provenance keys for effective input source locations.
    "source_params_path": os.environ["PARAMS_SOURCE_PATH"],
    "source_schedule_path": os.environ["SCHEDULE_SOURCE_PATH"],
    "source_analysis_schedule_path": os.environ["ANALYSIS_SCHEDULE_SOURCE_PATH"],
    # Deprecated legacy aliases; keep for backward compatibility while
    # downstream readers migrate to source_* keys.
    "params_source_path": os.environ["PARAMS_SOURCE_PATH"],
    "schedule_source_path": os.environ["SCHEDULE_SOURCE_PATH"],
    "analysis_schedule_source_path": os.environ["ANALYSIS_SCHEDULE_SOURCE_PATH"],
    "input_params_copy": os.environ["RUN_INPUT_PARAMS"],
    "input_schedule_copy": os.environ["RUN_INPUT_SCHEDULE"],
    "input_analysis_schedule_copy": os.environ["RUN_INPUT_ANALYSIS_SCHEDULE"],
    "out_root": os.environ["OUT_ROOT_USED"],
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
  local validation_global_path="artifacts/validation.json"
  local validation_run_path="${RUN_DIR}/validator.json"

  write_validation_from_status() {
    local status_str="fail"
    if [[ "${VALIDATOR_PASSED}" == "true" ]]; then
      status_str="pass"
    fi

    VALIDATION_STATUS="${status_str}" \
    VALIDATION_DETAILS="${VALIDATOR_DETAILS}" \
    VALIDATION_CMD="${VALIDATOR_CMD_USED}" \
    VALIDATION_RUN_DIR="${outdir}" \
    VALIDATION_RUN_LOG="${log_path}" \
    VALIDATION_GLOBAL_PATH="${validation_global_path}" \
    VALIDATION_RUN_PATH="${validation_run_path}" \
    python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

payload = {
    "run_dir": os.environ["VALIDATION_RUN_DIR"],
    "run_log": os.environ["VALIDATION_RUN_LOG"],
    "status": os.environ["VALIDATION_STATUS"],
    "details": os.environ["VALIDATION_DETAILS"],
    "validator_cmd": os.environ["VALIDATION_CMD"],
    "timestamp": datetime.now(timezone.utc).isoformat(),
}

global_path = Path(os.environ["VALIDATION_GLOBAL_PATH"])
run_path = Path(os.environ["VALIDATION_RUN_PATH"])
global_path.parent.mkdir(parents=True, exist_ok=True)
run_path.parent.mkdir(parents=True, exist_ok=True)
encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
global_path.write_text(encoded)
run_path.write_text(encoded)
PY
  }

  rm -f "${validation_global_path}" "${validation_run_path}"

  if [[ -f scripts/validate_smoke.py ]]; then
    VALIDATOR_CMD_USED="python3 scripts/validate_smoke.py --run_dir ${outdir} --out ${validation_global_path}"
    if python3 scripts/validate_smoke.py --run_dir "${outdir}" --out "${validation_global_path}" >"${RUN_DIR}/validator.log" 2>&1; then
      VALIDATOR_PASSED="true"
      VALIDATOR_DETAILS="python validator passed"
    else
      VALIDATOR_PASSED="false"
      VALIDATOR_DETAILS="python validator failed; see ${RUN_DIR}/validator.log and artifacts/validation.json"
    fi

    if [[ -f "${validation_global_path}" ]]; then
      cp "${validation_global_path}" "${validation_run_path}"
    else
      write_validation_from_status
    fi

    [[ "${VALIDATOR_PASSED}" == "true" ]] && return 0 || return 1
  fi

  if [[ -x tests/smoke_cosmo/validate_smoke.sh ]]; then
    VALIDATOR_CMD_USED="tests/smoke_cosmo/validate_smoke.sh ${outdir}"
    if tests/smoke_cosmo/validate_smoke.sh "${outdir}" >"${RUN_DIR}/validator.log" 2>&1; then
      VALIDATOR_PASSED="true"
      VALIDATOR_DETAILS="external shell validator passed"
      write_validation_from_status
      return 0
    else
      VALIDATOR_PASSED="false"
      VALIDATOR_DETAILS="external shell validator failed; see ${RUN_DIR}/validator.log"
      write_validation_from_status
      return 1
    fi
  fi

  if [[ -f tests/smoke_cosmo/validate_smoke.py ]]; then
    VALIDATOR_CMD_USED="python3 tests/smoke_cosmo/validate_smoke.py ${outdir}"
    if python3 tests/smoke_cosmo/validate_smoke.py "${outdir}" >"${RUN_DIR}/validator.log" 2>&1; then
      VALIDATOR_PASSED="true"
      VALIDATOR_DETAILS="external python validator passed"
      write_validation_from_status
      return 0
    else
      VALIDATOR_PASSED="false"
      VALIDATOR_DETAILS="external python validator failed; see ${RUN_DIR}/validator.log"
      write_validation_from_status
      return 1
    fi
  fi

  VALIDATOR_CMD_USED="builtin_validator"
  local snapshot_count
  snapshot_count="$(find "${outdir}" -type f \( -name "*.h5*" -o -name "*.bin*" -o -name "*.txt*" \) 2>/dev/null | wc -l | tr -d ' ')"

  if [[ "${snapshot_count}" -ge 1 ]] && grep -q "Saving Snapshot" "${log_path}" 2>/dev/null; then
    VALIDATOR_PASSED="true"
    VALIDATOR_DETAILS="builtin validator passed (found ${snapshot_count} output files and snapshot log markers)"
    write_validation_from_status
    return 0
  fi

  VALIDATOR_PASSED="false"
  VALIDATOR_DETAILS="builtin validator failed (found ${snapshot_count} output files)"
  write_validation_from_status
  return 1
}

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT_SHA_SHORT="$(git rev-parse --short=12 HEAD)"
fi

parse_args "$@"

write_env_colab

PARAMS_SOURCE_PATH="${EFFECTIVE_PARAMS}"
SCHEDULE_SOURCE_PATH="${EFFECTIVE_SCHEDULE}"
ANALYSIS_SCHEDULE_SOURCE_PATH="${EFFECTIVE_ANALYSIS_SCHEDULE}"

if [[ -z "${OUT_ROOT_USED}" ]]; then
  echo "Invalid --out-root (empty value)" >&2
  exit 2
fi

if [[ ! -f "${PARAMS_SOURCE_PATH}" ]]; then
  echo "Missing ${PARAMS_SOURCE_PATH}" >&2
  exit 2
fi
if [[ ! -f "${SCHEDULE_SOURCE_PATH}" ]]; then
  echo "Missing ${SCHEDULE_SOURCE_PATH}" >&2
  exit 2
fi
if [[ -n "${ANALYSIS_SCHEDULE_SOURCE_PATH}" ]] && [[ ! -f "${ANALYSIS_SCHEDULE_SOURCE_PATH}" ]]; then
  echo "Missing ${ANALYSIS_SCHEDULE_SOURCE_PATH}" >&2
  exit 2
fi

if [[ -n "${ANALYSIS_SCHEDULE_SOURCE_PATH}" ]]; then
  INPUT_HASH="$(cat "${PARAMS_SOURCE_PATH}" "${SCHEDULE_SOURCE_PATH}" "${ANALYSIS_SCHEDULE_SOURCE_PATH}" | sha256_stream)"
else
  INPUT_HASH="$(cat "${PARAMS_SOURCE_PATH}" "${SCHEDULE_SOURCE_PATH}" | sha256_stream)"
fi
if [[ -n "${RUN_ID_OVERRIDE}" ]]; then
  RUN_ID="${RUN_ID_OVERRIDE}"
else
  RUN_ID="${GIT_SHA_SHORT}_${RUN_PRESET}_${INPUT_HASH:0:16}"
fi

RUN_DIR="${OUT_ROOT_USED%/}/${RUN_ID}"
RUN_OUTDIR="${RUN_DIR}"
RUN_LOG="${RUN_DIR}/run.log"
RUN_INPUT_PARAMS="${RUN_DIR}/inputs/params.txt"
RUN_INPUT_SCHEDULE="${RUN_DIR}/inputs/scale_outputs.txt"
RUN_INPUT_ANALYSIS_SCHEDULE="${RUN_DIR}/inputs/analysis_scale_outputs.txt"
RUN_PARAMS="${RUN_DIR}/params.run.txt"
SCHEDULE_PATH="${RUN_INPUT_SCHEDULE}"
ANALYSIS_SCHEDULE_PATH=""

mkdir -p "${RUN_DIR}/inputs"
cp "${PARAMS_SOURCE_PATH}" "${RUN_INPUT_PARAMS}"
cp "${SCHEDULE_SOURCE_PATH}" "${RUN_INPUT_SCHEDULE}"
if [[ -n "${ANALYSIS_SCHEDULE_SOURCE_PATH}" ]]; then
  cp "${ANALYSIS_SCHEDULE_SOURCE_PATH}" "${RUN_INPUT_ANALYSIS_SCHEDULE}"
  ANALYSIS_SCHEDULE_PATH="${RUN_INPUT_ANALYSIS_SCHEDULE}"
fi

if [[ -f "${RUN_PRESET_DIR}/README.md" ]]; then
  cp "${RUN_PRESET_DIR}/README.md" "${RUN_DIR}/README.snapshot.txt"
elif [[ -f tests/smoke_cosmo/README.md ]]; then
  cp tests/smoke_cosmo/README.md "${RUN_DIR}/README.snapshot.txt"
fi

cp "${RUN_INPUT_PARAMS}" "${RUN_PARAMS}"
RUN_OUTDIR_PARAM="${RUN_OUTDIR}"
if [[ "${RUN_OUTDIR_PARAM}" != /* ]]; then
  RUN_OUTDIR_PARAM="./${RUN_OUTDIR_PARAM}"
fi
RUN_OUTDIR_PARAM="${RUN_OUTDIR_PARAM%/}/"

RUN_OUTDIR_ESCAPED="$(sed_escape_replacement "${RUN_OUTDIR_PARAM}")"
RUN_ID_ESCAPED="$(sed_escape_replacement "${RUN_ID}")"
RUN_INPUT_SCHEDULE_ESCAPED="$(sed_escape_replacement "${RUN_INPUT_SCHEDULE}")"
RUN_INPUT_ANALYSIS_SCHEDULE_ESCAPED="$(sed_escape_replacement "${RUN_INPUT_ANALYSIS_SCHEDULE}")"

sed -i.bak "s|RUN_OUTDIR|${RUN_OUTDIR_ESCAPED}|g" "${RUN_PARAMS}"
sed -i.bak "s|__RUN_ID__|${RUN_ID_ESCAPED}|g" "${RUN_PARAMS}"

if grep -q '^[[:space:]]*outdir=' "${RUN_PARAMS}"; then
  sed -i.bak "s|^[[:space:]]*outdir=.*$|outdir=${RUN_OUTDIR_ESCAPED}|g" "${RUN_PARAMS}"
else
  printf "\noutdir=%s\n" "${RUN_OUTDIR_PARAM}" >> "${RUN_PARAMS}"
fi

if grep -q '^[[:space:]]*scale_outputs_file=' "${RUN_PARAMS}"; then
  sed -i.bak "s|^[[:space:]]*scale_outputs_file=.*$|scale_outputs_file=${RUN_INPUT_SCHEDULE_ESCAPED}|g" "${RUN_PARAMS}"
else
  printf "\nscale_outputs_file=%s\n" "${RUN_INPUT_SCHEDULE}" >> "${RUN_PARAMS}"
fi

if [[ -n "${ANALYSIS_SCHEDULE_PATH}" ]]; then
  if grep -q '^[[:space:]]*analysis_scale_outputs_file=' "${RUN_PARAMS}"; then
    sed -i.bak "s|^[[:space:]]*analysis_scale_outputs_file=.*$|analysis_scale_outputs_file=${RUN_INPUT_ANALYSIS_SCHEDULE_ESCAPED}|g" "${RUN_PARAMS}"
  else
    printf "\nanalysis_scale_outputs_file=%s\n" "${RUN_INPUT_ANALYSIS_SCHEDULE}" >> "${RUN_PARAMS}"
  fi
fi

rm -f "${RUN_PARAMS}.bak"

echo "RUN_ID=${RUN_ID}"
echo "RUN_DIR=${RUN_DIR}"

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
