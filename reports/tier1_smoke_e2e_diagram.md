# Tier 1 Smoke End-to-End Diagram (Current, Implemented)

Scope: This is the end-to-end flow that Tier 1 Step 1 currently proves for a single-rank cosmology smoke run.

```mermaid
flowchart TD
    A[Fresh Colab runtime] --> B[Clone repo and checkout dev]
    B --> C[Preflight: ensure no absolute paths in tests/smoke_cosmo]
    C --> D[Run scripts/colab_smoke.sh]

    D --> D1[Capture env to artifacts/env_colab.json]
    D1 --> D2[Compute RUN_ID = git_sha + input_hash]
    D2 --> D3[Prepare run dir under runs/smoke_cosmo/<RUN_ID>]
    D3 --> D4{bin/cholla.cosmology.github exists?}
    D4 -->|No| D5[Build: CHOLLA_MACHINE=github make TYPE=cosmology -j2]
    D4 -->|Yes| D6[Use existing binary]
    D5 --> D7[Run binary with params.run.txt]
    D6 --> D7

    D7 --> D8[Write run.log and outputs]
    D8 --> D9[Run scripts/validate_smoke.py]
    D9 --> D10[Write artifacts/validation.json]
    D10 --> D11[Trap writes artifacts/run_manifest.json + artifacts/last_run_dir.txt]

    D11 --> E[Observe output tree and snapshot pattern]
    E --> F[Tighten validator patterns in scripts/validate_smoke.py]
    F --> G[Re-run validator: expect pass]
    G --> H[Delete one snapshot and re-run validator: expect fail]
    H --> I[Validate run_manifest.json against artifacts_schema/run_manifest_v0.json]
    I --> J[Run smoke second time, confirm stable RUN_ID]
    J --> K[Generate reports/tier1_step1_execution.md]
    K --> L[Commit allowed files and push]
```

## Proven outputs and gates
- Binary: `bin/cholla.cosmology.github`
- Run directory: `runs/smoke_cosmo/<RUN_ID>/`
- Snapshots: `**/*.h5.*` (example includes `0/0.h5.0`, `0/0_gravity.h5.0`, `0/0_particles.h5.0`)
- Hard-gate artifacts:
  - `artifacts/env_colab.json`
  - `artifacts/run_manifest.json`
  - `artifacts/validation.json`
- Quality gates:
  - validator pass on intact run
  - validator fail after deleting required snapshot
  - manifest schema valid
  - reproducible run identity for unchanged inputs

## Boundaries
- Single rank only (no `mpirun`)
- Smoke-scale problem (`8x8x8`, `tout=0.0`)
- Not a production campaign workflow
