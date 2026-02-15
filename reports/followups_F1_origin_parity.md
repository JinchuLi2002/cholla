# F1 Origin/Dev Parity + Clean-Clone Verification

- Local HEAD SHA: `b1768e66f6b548e72552cfa673ee50e6a4835176`
- origin/dev SHA after push: `4b91d758961c6121e9340299bb25585a99d1d582`
- Clean clone SHA: `4b91d758961c6121e9340299bb25585a99d1d582`
- scripts/test_smoke_overrides.sh exit code: `2`
- agent/run_agent.py --seed 123 --iters 3 exit code: `1`
- history.jsonl first 3 lines (verbatim):
```json
{"RUN_ID": "29d9ba05d311_smoke_cosmo_2323d7e33aeadbb9", "agent_run_id": "tier4_demo", "iteration": 0, "manifest_path": "/content/cholla/agent/runs/tier4_demo/iter_0/artifacts/run_manifest.json", "metric": {"name": "snapshot_file_count", "path": "/content/cholla/agent/runs/tier4_demo/iter_0/metric.json", "value": 3}, "params": {"H0": 61.9969877, "Init_redshift": 0.22254535, "Omega_L": 0.64394449, "Omega_M": 0.35362793, "gamma": 1.5342875, "nx": 10, "ny": 10, "nz": 10}, "seed": 123, "status": "success", "timestamp_utc": "2026-02-15T01:16:08Z"}
{"RUN_ID": "29d9ba05d311_smoke_cosmo_071c8efc8fbac48e", "agent_run_id": "tier4_demo", "iteration": 1, "manifest_path": "/content/cholla/agent/runs/tier4_demo/iter_1/artifacts/run_manifest.json", "metric": {"name": "snapshot_file_count", "path": "/content/cholla/agent/runs/tier4_demo/iter_1/metric.json", "value": 3}, "params": {"H0": 69.05289916, "Init_redshift": 0.04059268, "Omega_L": 0.67308949, "Omega_M": 0.31305877, "gamma": 1.12462244, "nx": 8, "ny": 8, "nz": 8}, "seed": 123, "status": "success", "timestamp_utc": "2026-02-15T01:16:09Z"}
{"RUN_ID": "29d9ba05d311_smoke_cosmo_27775db9389731e8", "agent_run_id": "tier4_demo", "iteration": 2, "manifest_path": "/content/cholla/agent/runs/tier4_demo/iter_2/artifacts/run_manifest.json", "metric": {"name": "snapshot_file_count", "path": "/content/cholla/agent/runs/tier4_demo/iter_2/metric.json", "value": 3}, "params": {"H0": 72.29490136, "Init_redshift": 0.02842643, "Omega_L": 0.77689672, "Omega_M": 0.26196284, "gamma": 1.07808572, "nx": 12, "ny": 12, "nz": 12}, "seed": 123, "status": "success", "timestamp_utc": "2026-02-15T01:16:10Z"}
```
- Notes (if any):
  - Parity check result after push: `git log --oneline origin/dev..dev` is empty.
  - Clean-clone command logs were captured under `/var/folders/1g/wc8l7sb96qs9q8zydq6qwk5w0000gn/T/tmp.l2QLdAKMKi`.
  - `scripts/test_smoke_overrides.sh` failed in clean clone because `mpicxx` was not found (`bash: mpicxx: command not found`).
  - `agent/run_agent.py --seed 123 --iters 3` completed iterations but returned non-zero because all three iterations were marked failed in this environment (`iters_succeeded=0`, `iters_failed=3`).
  - RUN_IDs produced by clean-clone `run_agent.py` execution:
    - `4b91d758961c_smoke_cosmo_2323d7e33aeadbb9`
    - `4b91d758961c_smoke_cosmo_071c8efc8fbac48e`
    - `4b91d758961c_smoke_cosmo_27775db9389731e8`
