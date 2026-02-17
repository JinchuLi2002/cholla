# Phase 3D Kernel Replay Invariants

## Scope and Authority
- Scope: `python -m agent.controller.run_phase3c` acceptance and replay-only flows.
- Authoritative CLI module: `agent/controller/run_phase3c.py` (`main`, `_run_acceptance_mode`, `_run_replay_only_mode`).
- Acceptance output contract includes `strict_replay` when `--acceptance_replay_strict` is enabled.
- Authoritative replay implementation: `agent/controller/controller.py` (`HybridController.replay`).
- Authoritative controller history writer: `agent/controller/history.py` (`HistoryWriter.append`).
- Linked (optional) replay gate: `agent/tools/replay_check.py` calls `agent/run_agent.py::_run_replay_mode` when `linked_experiment_id` is set in controller bundle config.

## Replay-Critical Fields (JSON Pointer Style)

### A) Controller bundle (`agent/experiments/<experiment_id>/controller/`)

#### `config.json`
- `/experiment_id`
  - Required for replay-only canonical path check in `run_phase3c`.
- `/controller_run_id`
  - Required by `HybridController.replay`.
- `/tool_backend`
  - Required by replay-only flow to rebuild tool registry.
- `/linked_experiment_id`
  - If non-empty string, strict replay must also pass linked `replay_check`.
- `/config_effective_path`
  - Contract-critical when present: must equal `"config_effective.yaml"` and that file must exist.

#### `history_controller.jsonl` (records where `/record_type=="iteration"`)
- `/record_type` must be `"iteration"`.
- `/iteration` must be integer; replay sorts by this field.
- `/summary` (entire object) must validate `SummarySpecV0`.
- `/plan` (entire object) must validate `PlanSpecV0`.
- `/plan/should_stop` controls whether tools are replayed.
- `/RUN_ID`
  - Required non-empty string when tool replay is performed.
- `/tool_results`
  - Order-critical by call index and tool role.
- `/tool_results/<i>/tool=="validate_params"`:
  - `/tool_results/<i>/payload/params` must match expected params from plan.
- `/tool_results/<j>/tool=="run_cholla"`:
  - `/tool_results/<j>/status` must be `"success"` for strict tool replay path.
  - `/tool_results/<j>/result/run_id` must equal `/RUN_ID`.
- `/tool_results/<k>/tool=="compute_metric"`:
  - `/tool_results/<k>/status` must be `"success"` for strict tool replay path.
  - `/tool_results/<k>/result/scalar` (or fallback `/metric_value`) must match replay scalar.

#### `param_space.yaml`
- Full payload is replay-critical.
- Template path keys are replay-critical:
  - `/template_params_path`
  - `/template_schedule_path`
- Parameter spec content used for validation is replay-critical:
  - `/parameters/*` (names/types/defaults/bounds).

#### `iterations/iter_<n>.json`
- Replay reads from `history_controller.jsonl`, not `iterations/*.json`.
- These files are still contract-critical snapshots: they must be consistent projections of corresponding history iteration rows.

### B) Linked experiment bundle (optional, via `replay_check`)

When `controller/config.json` has non-empty `/linked_experiment_id`, strict replay additionally requires:
- `agent/experiments/<linked_experiment_id>/history_experiment.jsonl` (preferred) or `history.jsonl`.
- `agent/experiments/<linked_experiment_id>/summary.json` `/git_sha`.
- Iteration rows in linked history:
  - `/record_type=="iteration"`
  - `/iteration`
  - `/status`
  - `/params`
  - `/RUN_ID`

This is why `history.jsonl` remains part of replay contract surface even for Phase 3D controller runs.

## `run_id` and Hash-Derived Fields

### Controller replay (`history_controller.jsonl` `/RUN_ID`)
- Source of truth in controller history:
  - `HybridController._run_id_from_tool_results` extracts `run_cholla` result `/run_id`.
- In strict replay:
  - Replayed `run_cholla` `run_id` must equal history `/RUN_ID`.
  - Stored `run_cholla` `result/run_id` must equal history `/RUN_ID`.

### Backend-specific `run_id` derivation
- Real backend path (`agent/tools/run_cholla.py` -> `scripts/colab_smoke.sh`):
  - Controller passes explicit payload `run_id = "<controller_run_id>_iter_<NNNN>"`.
  - Script uses override when provided (`--run-id`).
  - If override is absent, script fallback is:
    - `run_id = "<git_sha_short>_smoke_cosmo_<sha256(cat(params_file,schedule_file))[:16]>"`
- Mock backend path (`agent/tools/mock_backend.py::_run_id_from_text`):
  - Ignores payload `run_id`.
  - Computes:
    - `run_id = "mock_" + sha256(params_text + "\n<schedule>\n" + schedule_text)[:12]`

### Other hash-derived fields written under `agent/experiments/**`
- `tasks/task_<n>_{planner|summarizer}.json` `/prompt_input_hash`:
  - `sha256(json.dumps({"system_prompt": <prompt>, "input": <payload>}, sort_keys=True, separators=(",", ":"), default=str))`
- Replay backend temp-root hash (affects paths in replay artifacts, not bundle tree):
  - `sha1(f"{experiment_id}:{controller_run_id}:{replay_mode}")[:10]`

### Linked `run_agent` replay `RUN_ID` formula
- `agent/run_agent.py::_run_id_from_params`:
  - `expected_run_id = "<git_sha[:12] or unknown>_smoke_cosmo_<sha256(params_text_utf8 + schedule_bytes)[:16]>"`
- Used by linked `replay_check` and therefore transitively strict-replay-critical when linked.

## Canonical Serialization and Ordering Rules

### JSON files
- Use `json.dumps(payload, indent=2, sort_keys=True) + "\n"`.
- Applied by controller bundle writer and most tool manifests.

### JSONL files
- One object per line, no indentation:
  - `json.dumps(record, sort_keys=True) + "\n"`.
- Applied to `history_controller.jsonl`, bundle history mirrors, and controller history appends.

### Hash input canonicalization
- For hash payloads, use:
  - `sort_keys=True`
  - `separators=(",", ":")`
  - `default=str` only where code already uses it.

### Stable list ordering
- Replay iteration order: sort by `/iteration`.
- `tool_results` order: deterministic chain (`validate_params` -> `run_cholla` -> `compute_metric`).
- Produced-file enumerations: sorted by path string in wrappers.
- Added params in rendered params text: appended in sorted key order.
- Glob-based candidate lists in metric code: sorted before selection.

### YAML
- `param_space.yaml` and `config_effective.yaml` are emitted with `yaml.safe_dump(..., sort_keys=True)`.

## Bundle Tree Contract (Required Files)

For each Phase 3D experiment id `<E>`:
- Required:
  - `agent/experiments/<E>/controller/config.json`
  - `agent/experiments/<E>/controller/history_controller.jsonl`
  - `agent/experiments/<E>/controller/param_space.yaml`
  - `agent/experiments/<E>/controller/summary.json`
  - `agent/experiments/<E>/controller/iterations/` with `iter_<n>.json` for each iteration record.
  - `agent/experiments/<E>/tasks/` (task envelopes written per attempted agent call).
- Conditional:
  - `agent/experiments/<E>/controller/config_effective.yaml` iff `config.json` includes `/config_effective_path`.
  - `agent/experiments/<E>/controller/replay/strict/**` only after strict replay runs (for example via `--acceptance_replay_strict`).
  - `agent/experiments/<E>/controller/replay/live/**` only after live replay runs.

## Forbidden Nondeterminism Sources (and Current Mitigation)

- Time-derived IDs in contract-critical identity fields.
  - Forbidden: implicit `controller_run_id` / `experiment_id` defaults from current UTC time.
  - Mitigation: set both explicitly in config for reproducible bundles.
- Unsorted filesystem traversal for replay-relevant selection.
  - Forbidden: using raw glob/find iteration order for metric/log/snapshot decisions.
  - Mitigation: metric and manifest readers sort candidates before selection.
- Unstable dict key emission.
  - Forbidden: writing replay-relevant JSON without key sorting.
  - Mitigation: bundle/history/tool writers use `sort_keys=True`.
- Hidden random paths in replay-critical fields.
  - Forbidden: random temp paths in fields used for strict comparison.
  - Mitigation today: strict replay compares params, RUN_ID, metric scalar; path-like fields are not replay checks.
  - Residual risk: real `run_cholla` staging paths include random `mkdtemp` suffixes and will differ in non-normalized bundle diffs.
- Backend divergence on `run_id` override semantics.
  - Forbidden: backend changing `run_id` behavior without updating replay contract.
  - Mitigation today: strict replay enforces equality between history RUN_ID and replayed run_cholla RUN_ID (backend-agnostic).

## Allowed Benign Diffs (Target: Empty After Normalization)

Allowed only when explicitly normalized out by diff tooling:
- Any `/timestamp_utc` values.
- Absolute path prefixes in path fields (`/artifact_paths/*`, `/run_dir`, `/history_path`, replay temp roots).
- Real-backend staging/log file paths from `run_cholla` temp directories.

All other diffs in replay-critical pointers are failures.

## Concrete Example: One History Iteration Row

Example fragment (from `history_controller.jsonl`):

```json
{
  "record_type": "iteration",
  "iteration": 0,
  "RUN_ID": "mock_ccc4c2bcf0d5",
  "plan": {
    "should_stop": false,
    "metadata": {"proposed_params": {"Init_redshift": 0.0, "nx": 12}}
  },
  "tool_results": [
    {"tool": "validate_params", "payload": {"params": {"Init_redshift": 0.0, "nx": 12}}},
    {"tool": "run_cholla", "status": "success", "result": {"run_id": "mock_ccc4c2bcf0d5"}},
    {"tool": "compute_metric", "status": "success", "result": {"scalar": 0.0}}
  ]
}
```

Replay-critical pointers in this row:
- `/record_type`
- `/iteration`
- `/plan/should_stop`
- `/plan/metadata/proposed_params`
- `/RUN_ID`
- `/tool_results/0/payload/params`
- `/tool_results/1/status`
- `/tool_results/1/result/run_id`
- `/tool_results/2/status`
- `/tool_results/2/result/scalar` (or `/tool_results/2/result/metric_value`)

## Diff Checklist (M0-4 Mapping)

1. M0-1 Contract freeze:
- Confirm this document is treated as normative for replay-critical pointers and bundle files.

2. M0-2 Canonicalization freeze:
- Verify all writers still use sorted-key JSON/JSONL and sorted traversal where specified.

3. M0-3 Structural diff gate:
- Check required bundle tree and conditional file rules (`config_effective` rule included).
- Normalize only explicitly allowed benign diffs.

4. M0-4 Replay equivalence gate:
- Run strict replay and require status `ok`.
- Require unchanged `iterations_checked`, `params_checked`, `run_ids_checked`, `metrics_checked`.
- For linked experiments, require linked `replay_check.status == "ok"` (covers linked `history.jsonl` / `RUN_ID` checks).
