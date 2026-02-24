# Cosmology Smoke Case Inputs (Code-Confirmed)

This file records only parser/runtime behavior confirmed from source code. No doc-only assumptions are used.

## Confirmed Parameter Key Names (Parser)

All keys below are parsed in `Parse_Param`:

- Grid size: `nx`, `ny`, `nz` (`src/global/global.cpp:195-200`)
- Runtime/output timing: `tout`, `outstep`, `n_steps_output` (`src/global/global.cpp:205-210`)
- Initializer selector: `init` (`src/global/global.cpp:213-214`)
- Restart file index: `nfile` (`src/global/global.cpp:215-216`)
- Domain lengths/origin: `xmin`, `ymin`, `zmin`, `xlen`, `ylen`, `zlen` (`src/global/global.cpp:263-274`)
- Output/input directories: `outdir`, `indir` (`src/global/global.cpp:289-292`)
- Cosmology schedule/end keys: `scale_outputs_file`, `End_redshift` (`src/global/global.cpp:404-409`)
- Other cosmology keys used by initializer: `Init_redshift`, `H0`, `Omega_M`, `Omega_L`, `Omega_b` (`src/global/global.cpp:406-417`)

`scale_outputs_file` is explicitly initialized to empty string before parsing in COSMOLOGY builds (`src/global/global.cpp:138-141`).

## Runtime Requirements in Cosmology Mode

### Core run controls

- `init` is required to match a known mode; unknown value aborts (`src/grid/initial_conditions.cpp:34-35`, `src/grid/initial_conditions.cpp:97-100`).
- `gamma` must be `> 1`; enforced by `Set_Gammas` during IC setup (`src/grid/initial_conditions.cpp:32`, `src/global/global.cpp:33-38`).
- `tout` controls main loop termination (`src/main.cpp:247`).
- `outstep` is used to advance output schedule (`src/main.cpp:223-224`, `src/main.cpp:349`).
- `nx`, `ny`, `nz` are consumed to build grid dimensions (`src/grid/grid3D.cpp:145-177`) and invalid resulting `n_cells` aborts (`src/grid/grid3D.cpp:191-195`).
- `xmin/ymin/zmin` and `xlen/ylen/zlen` are consumed to set domain geometry and `dx/dy/dz` (`src/grid/initial_conditions.cpp:109-175`).

### Cosmology-specific controls

- Cosmology initialization consumes `H0`, `Omega_M`, `Omega_L`, `Omega_b` (`src/cosmology/cosmology.cpp:13-20`).
- If `init != Read_Grid`, cosmology start uses `Init_redshift` (`src/cosmology/cosmology.cpp:21-27`).
- Cosmology normalization uses `xlen` and `nx` (`src/cosmology/cosmology.cpp:49`).

### Restart/file-backed initializers

- `init=Read_Grid` reads hydro IC/restart from `indir + nfile` (plus extension) (`src/io/io.cpp:2258-2265`, `src/io/io.cpp:2302-2305`).
- `init=Read_Grid_Cat` reads from `sprintf("%s%d.h5", indir, nfile)` (`src/io/io_parallel.cpp:65-71`).
- With `PARTICLES`, `init=Read_Grid` also loads particle data from `indir` and `nfile` (`src/particles/particles_3D.cpp:203-204`, `src/particles/io_particles.cpp:39-45`, `src/particles/io_particles.cpp:53-57`).

## Cosmology Schedule Key, End Conditions, and File Format

- **Schedule key name:** `scale_outputs_file` (`src/global/global.cpp:404-405`, `src/cosmology/io_cosmology.cpp:13`).
- **Schedule file format:** each line is read and converted with `atof(line.c_str())`; values are pushed directly (`src/cosmology/io_cosmology.cpp:20-23`). No comment/format validation is implemented there.
- **Can `End_redshift` be used instead of `scale_outputs_file`?** **Yes.** If `scale_outputs_file` is empty, code computes `scale_end = 1/(End_redshift+1)` and builds outputs from current scale factor plus `scale_end` (`src/cosmology/io_cosmology.cpp:55-60`).
- **End condition options present in code:**
  - Time-based: `while (t < tout)` (`src/main.cpp:247`)
  - Cosmology-output-based: break when `Cosmo.exit_now` (`src/main.cpp:367-372`), where `exit_now` is set when scale-output list is exhausted (`src/cosmology/io_cosmology.cpp:81-87`).

## Built-in Initializers (No External IC File)

Hydro initializer options are dispatched in `Set_Initial_Conditions` (`src/grid/initial_conditions.cpp:34-95`).

- File-backed modes: `Read_Grid`, `Read_Grid_Cat` (`src/grid/initial_conditions.cpp:72-80`).
- Built-in (non-file) examples: `Uniform`, `Constant`, `Riemann`, `Linear_Wave`, etc. (`src/grid/initial_conditions.cpp:34-71`, `src/grid/initial_conditions.cpp:81-95`).
- Portable built-in choice for smoke:
  - `init=Uniform` uses in-memory hydro setup (`src/grid/initial_conditions.cpp:81-82`, `src/grid/initial_conditions.cpp:1429-1471`).
  - With `PARTICLES`, `init=Uniform` also has built-in particle initialization (`src/particles/particles_3D.cpp:40-42`, `src/particles/particles_3D.cpp:810-869`).

## Tier 1 Step 0 Note

Tier 1 Step 0 goal = **1 output dump; scientific correctness not required**.

Related precedent: io system test generates restart source data with `tout=0.0 outstep=0.0` for a one-dump setup (`src/io/io_tests.cpp:27-31`).

## IC Strategy Selected for Portable Smoke Inputs

- Selected strategy: built-in `init=Uniform` (no external IC files).
- Code basis: `init=Uniform` dispatch is built-in (`src/grid/initial_conditions.cpp:81-82`) and initializes hydro data in memory (`src/grid/initial_conditions.cpp:1429-1471`).
- For builds with particles, `init=Uniform` also provides built-in particle initialization (`src/particles/particles_3D.cpp:40-42`, `src/particles/particles_3D.cpp:810-869`).
- Therefore `tests/smoke_cosmo/ics/` is intentionally not created for Step 0 portability.
