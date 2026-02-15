# Full Production Cosmology End-to-End Diagram (Target)

Scope: This is the full workflow needed for real science production runs. It includes what Tier 1 Step 1 already covers and what is still missing.

```mermaid
flowchart TD
    A[Science goal and acceptance criteria] --> B[Select physics model and compile flags]
    B --> C[Define campaign design: box, resolution, redshift range, outputs]
    C --> D[Generate or acquire initial conditions]
    D --> E[Stage IC/restart data to target storage]
    E --> F[Author production params and output schedule]
    F --> G[Build Cholla for target machine/toolchain]

    G --> H[Preflight checks: env, paths, dependencies, I/O quotas]
    H --> I[Pilot run and validation on small allocation]
    I --> J{Pilot passes physics + I/O checks?}
    J -->|No| F
    J -->|Yes| K[Launch full multi-rank production run]

    K --> L[Checkpoint/restart management]
    L --> M[Monitoring: throughput, stability, failures]
    M --> N{Run interruption?}
    N -->|Yes| L
    N -->|No| O[Complete target redshift/time]

    O --> P[Post-processing and derived products]
    P --> Q[Scientific QA and consistency checks]
    Q --> R[Archive outputs, metadata, provenance]
    R --> S[Publish report and reproducibility bundle]
```

## Coverage status relative to Tier 1 Step 1
- Covered now:
  - Fresh runtime execution path
  - Cosmology binary build
  - Single-rank smoke run
  - Output validation and fail-on-deletion check
  - Manifest/schema/env artifact capture
  - Reproducibility check for unchanged smoke inputs
- Not covered yet:
  - Large IC generation/staging workflow
  - Multi-rank (`mpirun`) decomposition and scaling
  - Scheduler integration (SLURM/PBS) and walltime strategy
  - Long-run checkpoint/restart operations
  - Performance tuning and cost modeling
  - Production-scale QA metrics and science sign-off pipeline
  - Long-term archiving and dataset publication procedures

## Practical interpretation
Tier 1 Step 1 validates the smoke execution backbone. It should be treated as the reliability foundation for the production pipeline, not the production pipeline itself.
