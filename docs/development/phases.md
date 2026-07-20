# Phase log

`bikescore` was carved out of `bna-core` in **Phase 38** — the scoring-library /
orchestration split. The core is a clean-slate port, module by module, validated against
the frozen `bna-core` oracle on Aspen as each piece landed. The A-series below is the core
repo's own history; the parallel B-series (the `bikescore-app` orchestration layer) lives
in that repo's log.

## Phase 38 A-series — the core

| Sub-phase | What landed |
|---|---|
| **38a** | Oracle baseline snapshot + `bikescore` scaffold + docs skeleton |
| **38b** | Foundation port (config, scenarios, decision DSL) + `build_config()` |
| **38c** | `StageSpec` + `run_stage` + `PIPELINE` + `score_city` skeleton |
| **38d** | Walking-skeleton stages `parse → stress` with per-stage parity |
| **38e** | Scoring stages `graph → neighborhood` |
| **38f** | End-to-end `score_city` parity — the crux gate: identical Aspen output vs the oracle |
| **38g** | `acquire_city` / `InputProvider`, the `bikescore-score` CLI, release `v0.1.0`, and the core docs |

At **38g** the core became independently installable and validatable: `score_city`
reproduces the oracle on Aspen with no database, workspace, or web layer, and the full
suite is green on a cold clone.

## After the split

Feature work resumes on the core once it reaches parity. Landed so far:

- **Export** — a database-free export framework (`bikescore.export`) that writes any stage
  output (road-segment LTS, census blocks, destinations, connectivity, …) to
  GeoJSON / Shapefile / CSV from a `score_city` result, plus the `bna` deliverable bundle
  and the `bikescore-score export` / `export-list` CLI commands. See
  [Output files](../reference/output-files.md#export) and the [CLI](../reference/cli.md#export).

The authoritative, blow-by-blow phase briefs and retrospectives for the split live in the
frozen `bna-core` repo under `phases/38*.md`.
