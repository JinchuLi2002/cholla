# Backend Contract: `scripts/colab_smoke.sh`

This document defines the Tier-1/Tier-4 backend contract as implemented today.

## 1) CLI interface

Command:

```bash
bash scripts/colab_smoke.sh \
  [--params PATH] \
  [--schedule PATH] \
  [--out-root PATH] \
  [--run-id RUN_ID]
```

Flags and defaults:

- `--params PATH`
  - Default: `tests/smoke_cosmo/params.txt`
  - Meaning: source params template for this run.
- `--schedule PATH`
  - Default: `tests/smoke_cosmo/scale_outputs.txt`
  - Meaning: source schedule file for this run.
- `--out-root PATH`
  - Default: `runs/smoke_cosmo`
  - Meaning: root directory where run directories are created.
- `--run-id RUN_ID`
  - Default: computed (see RUN_ID semantics below).
  - Meaning: explicit run directory identifier override.

Examples:

- Default run:
```bash
bash scripts/colab_smoke.sh
```

- Override params (without mutating template):
```bash
bash scripts/colab_smoke.sh --params /tmp/params_variant.txt
```

## 2) Artifacts emitted

Global artifacts (repo-local cache):

- `artifacts/run_manifest.json`
- `artifacts/validation.json`
- `artifacts/env_colab.json`
- `artifacts/last_run_dir.txt`

Run-local artifacts under `<RUN_DIR>`:

- `<RUN_DIR>/inputs/params.txt` (copied effective params source)
- `<RUN_DIR>/inputs/scale_outputs.txt` (copied effective schedule source)
- `<RUN_DIR>/params.run.txt` (run-local params used by the binary)
- `<RUN_DIR>/run.log`
- `<RUN_DIR>/validator.log`
- `<RUN_DIR>/validator.json` (run-local validation copy)
- `<RUN_DIR>/README.snapshot.txt`

Notes:

- Placeholder/outdir replacement is applied to run-local `params.run.txt`, not to the source template.
- `artifacts/validation.json` is a global cache; `<RUN_DIR>/validator.json` is immutable per-run provenance.

## 3) Manifest field semantics (`artifacts/run_manifest.json`)

Schema-required top-level fields:

- `git_sha`: short git SHA used by script.
- `build_command`: build command string used.
- `binary_path`: binary actually executed.
- `params_path`: run-local params path used for execution.
- `schedule_path`: run-local schedule path used for execution.
- `run_dir`: concrete run directory.
- `produced_files`: files found under `run_dir` at script exit.
- `validation`: object with `status` (`pass|fail`) and `details`.
- `error`: `null` on success, otherwise failure message.

Important additional fields currently written:

- `status`, `exit_code`, `run_id`, `run_log`, `run_params`, `input_hash_sha256`
- Canonical provenance keys:
  - `source_params_path`, `source_schedule_path` (original source paths passed/effective)
- Deprecated compatibility aliases:
  - `params_source_path`, `schedule_source_path`
- Run-local input copy paths:
  - `input_params_copy`, `input_schedule_copy`
- Validator summaries:
  - `validator_passed`, `validator_details`, `validator_cmd`

Source vs run-local paths:

- Source: `source_params_path`, `source_schedule_path`
- Run-local copies: `input_params_copy`, `input_schedule_copy`
- Executed params file: `params_path` (`<RUN_DIR>/params.run.txt`)

Error semantics:

- Success: `status="success"`, `exit_code=0`, `error=null`
- Failure: `status="failed"`, `exit_code!=0`, `error` contains command/exit context

## 4) RUN_ID semantics and determinism

Default RUN_ID:

- `RUN_ID = <git_sha_short>_smoke_cosmo_<first16(sha256(params_contents + schedule_contents))>`
- Hash input is content-based (`cat params schedule | sha256`), so path changes alone do not change RUN_ID.

Override RUN_ID:

- If `--run-id` is provided, that value is used directly.

Determinism expectations:

- Same git SHA + same effective params content + same effective schedule content + no `--run-id` => same RUN_ID.
- Different params/schedule content => different default RUN_ID.
- `--run-id` intentionally bypasses content-derived RUN_ID.
