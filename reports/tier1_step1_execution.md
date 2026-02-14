# Tier 1 Step 1 Execution Report

Generated UTC: 2026-02-14T21:24:53.853403+00:00

## Commands Run
- `rm -rf cholla`
- `git clone https://github.com/JinchuLi2002/cholla.git`
- `git checkout dev`
- `git pull --ff-only`
- `bash scripts/colab_smoke.sh` (single-rank)
- `python3 scripts/validate_smoke.py --run_dir "runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce"`
- `python3 scripts/validate_smoke.py --run_dir "runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce"` after deleting one snapshot
- `bash scripts/colab_smoke.sh` (repro run)

## Exit Codes
- smoke_exit_latest: 0
- validator_exit_before_delete: 0
- validator_exit_after_delete: 1

## Run Identity
- RUN_ID: `731cd0021ca0_ab14588df653bbce`
- run_dir: `runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce`
- run_id_stable_second_run: `True`

## Output Tree Snippet (first ~200 lines)
```text
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/0
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/0/0_gravity.h5.0
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/0/0.h5.0
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/0/0_particles.h5.0
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/params.run.txt
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/params.txt
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/README.snapshot.txt
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/run.log
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/scale_outputs.txt
runs/smoke_cosmo/731cd0021ca0_ab14588df653bbce/validator.log

```

## Observed Snapshot Pattern
- first_snapshot_rel: `0/0.h5.0`
- snapshot_glob: `**/*.h5.*`

## Validator Checks
- validator_exit_before_delete=0
- validator_exit_after_delete=1
- Deletion-failure proof: validator must fail after deleting one snapshot file.

## Artifact Presence And Summary
- run_manifest_exists: `True`
- validation_exists: `True`
- env_colab_exists: `False`
- run_manifest.validation.status: `pass`
- validation.status: `pass`

### env_colab.json summary
- git_sha: <missing>
- gpu_name: <missing>
- driver_version: <missing>
- cuda_version: <missing>
- gcc_version: <missing>
- os_release: <missing>
- cholla_machine: <missing>

## Schema Validation
- SCHEMA_VALID=OK

## Smoke Input Adjustments
- None in this run unless explicitly edited in Failure Loop section.
